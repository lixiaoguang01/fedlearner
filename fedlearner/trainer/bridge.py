# Copyright 2020 The FedLearner Authors. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# coding: utf-8
# pylint: disable=protected-access

import os
import collections
import logging
import threading
import time

import tensorflow.compat.v1 as tf
from google.protobuf import any_pb2 as any_pb
# TODO Wait for PChannel to finish
from fedlearner.channel import Channel

from fedlearner.common import common_pb2 as common_pb
from fedlearner.common import trainer_worker_service_pb2 as tws2_pb
from fedlearner.common import trainer_worker_service_pb2_grpc as tws2_grpc

class Bridge(object):
    class TrainerWorkerServicer(tws2_grpc.TrainerWorkerServiceServicer):
        def __init__(self, bridge):
            super(Bridge.TrainerWorkerServicer, self).__init__()
            self._bridge = bridge

        def StreamTransmit(self, request_iterator, context):
            for request in request_iterator:
                yield self._bridge._transmit_handler(request)

        def LoadDataBlock(self, request, context):
            return self._bridge._data_block_handler(request)

    def __init__(self,
                 role,
                 listen_port,
                 remote_address,
                 app_id=None,
                 rank=0,
                 stream_queue_size=1024,
                 waiting_alert_timeout=10):
        self._role = role
        self._listen_address = "[::]:{}".format(listen_port)
        self._remote_address = remote_address
        if app_id is None:
            app_id = 'test_trainer'
        self._token = "{}-{}".format(app_id, rank)

        self._condition = threading.Condition()
        self._started = False
        self._terminated = False
        self._peer_terminated = False

        self._current_iter_id = None
        self._next_iter_id = 0
        self._peer_start_iter_id = None
        self._peer_commit_iter_id = None

        self._received_data = collections.defaultdict(dict)
        self._data_block_handler_fn = None

        self._waiting_alert_timeout = waiting_alert_timeout
        if self._waiting_alert_timeout < 1:
            self._waiting_alert_timeout = 1

        # transmit stream queue
        self._stream_queue = collections.deque()
        self._stream_queue_size = stream_queue_size
        self._stream_thread = threading.Thread(
            target=self._stream_fn, daemon=True)

        # channel
        self._channel = Channel(
            self._listen_address, self._remote_address, token=self._token)
        self._channel.subscribe(self._channel_callback)

        # client & server
        self._client = tws2_grpc.TrainerWorkerServiceStub(self._channel)
        tws2_grpc.add_TrainerWorkerServiceServicer_to_server(
            Bridge.TrainerWorkerServicer(self), self._channel)

    def _channel_callback(self, channel, event):
        if event == Channel.Event.PEER_CLOSED:
            with self._condition:
                self._peer_terminated = True
                self._condition.notify_all()
        if event == Channel.Event.ERROR:
            err = channel.error()
            logging.fatal("[Bridge] suicide as channel exception: %s,"
                " maybe caused by peer restart", repr(err))
            os._exit(138)  # Tell Scheduler to restart myself

    @property
    def current_iter_id(self):
        return self._current_iter_id

    @property
    def next_iter_id(self):
        return self._next_iter_id

    @property
    def role(self):
        return self._role

    @property
    def connected_at(self):
        return self._channel._connected_at

    @property
    def terminated_at(self):
        return self._channel._closed_at

    def connect(self):
        with self._condition:
            if self._started:
                return
            self._started = True
            self._channel.connect()
            self._stream_thread.start()

    def terminate(self):
        with self._condition:
            if not self._started:
                return
            if self._terminated:
                return
            self._terminated = True
            self._condition.notify_all()
        self._stream_thread.join()
        self._channel.close()

    def _stream_fn(self):
        stream_response = self._client.StreamTransmit(self._stream_generator())
        for _ in stream_response:
            pass

    def _stream_generator(self):
        while True:
            with self._condition:
                while len(self._stream_queue) == 0:
                    if self._terminated:
                        return
                    self._condition.wait()
                msg = self._stream_queue.popleft()
                self._condition.notify_all()
            yield msg

    def _transmit(self, msg):
        with self._condition:
            if self._terminated:
                raise RuntimeError("[Bridge] transmit stream was terminated")
            while len(self._stream_queue) == self._stream_queue_size:
                logging.warning("[Bridge] transmit stream queue is full"
                                ", size: %d", len(self._stream_queue))
                self._condition.wait()
            self._stream_queue.append(msg)
            self._condition.notify_all()

    def _transmit_handler(self, request):
        with self._condition:
            if request.HasField("start"):
                if self._peer_commit_iter_id is not None \
                    and request.start.iter_id <= self._peer_commit_iter_id:
                    logging.warning(
                        "[Bridge] received peer start iter_id: %d"
                        " which has been committed."
                        " maybe caused by resend.(peer_commit_iter_id: %d)",
                        request.start.iter_id, self._peer_commit_iter_id)
                elif self._peer_start_iter_id is not None \
                    and request.start.iter_id <= self._peer_start_iter_id:
                    logging.warning(
                        "[Bridge] received repeated peer start iter_id: %d."
                        " maybe caused by resend."
                        "(peer_start_iter_id: %d)",
                        request.start.iter_id, self._peer_start_iter_id)
                else:
                    logging.debug("[Bridge] received peer start iter_id: %d",
                        request.start.iter_id)
                    self._peer_start_iter_id = request.start.iter_id
                    self._condition.notify_all()

            elif request.HasField("data"):
                if self._peer_start_iter_id is None:
                    logging.warning(
                        "[Bridge] received data iter_id: %d without start."
                        " maybe caused by resend.",
                        request.data.iter_id)
                elif self._peer_start_iter_id != request.data.iter_id:
                    logging.warning(
                        "[Bridge] received data iter_id: %d no match start."
                        " maybe caused by resend.(peer_start_iter_id: %d)",
                        request.data.iter_id, self._peer_start_iter_id)
                else:
                    iter_id = self._current_iter_id \
                        if self._current_iter_id is not None \
                            else self._next_iter_id
                    if request.data.iter_id < iter_id:
                        logging.debug("[Bridge] received data iter_id: %d, "
                            "name: %s, ignored by our commit."
                            "(current_iter_id: %s, next_iter_id: %d)",
                            request.data.iter_id, request.data.name,
                            self._current_iter_id, self._next_iter_id)
                    else:
                        logging.debug("[Bridge] received data iter_id: %d,"
                            " name: %s",
                            request.data.iter_id, request.data.name)
                        self._received_data[ \
                            request.data.iter_id][ \
                                request.data.name] = request.data
                        self._condition.notify_all()

            elif request.HasField("commit"):
                if self._peer_commit_iter_id is not None \
                    and request.commit.iter_id <= self._peer_commit_iter_id:
                    logging.warning(
                        "[Bridge] receive repeated peer commit iter_id: %d."
                        " maybe caused by resend.(peer_commit_iter_id: %d)",
                        request.commit.iter_id, self._peer_commit_iter_id)
                elif self._peer_start_iter_id is None:
                    logging.error(
                        "[Bridge] receive peer commit iter_id: %d"
                        " without start",
                        request.commit.iter_id)
                    # Fatal or return error?
                elif request.commit.iter_id != self._peer_start_iter_id:
                    logging.error(
                        "[Bridge] receive peer commit iter_id: %s"
                        " no match start.(peer_start_iter_id: %d)",
                        request.commit.iter_id, self._peer_start_iter_id)
                    # Fatal or return error?
                else:
                    logging.debug("[Bridge] receive peer commit iter_id: %d",
                        request.commit.iter_id)
                    self._peer_start_iter_id = None
                    self._peer_commit_iter_id = request.commit.iter_id
                    self._condition.notify_all()

        return tws2_pb.TransmitResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS))

    def _data_block_handler(self, request):
        assert self._data_block_handler_fn is not None, \
            "[Bridge] receive DataBlockMessage but no handler registered."
        if self._data_block_handler_fn(request):
            logging.info('[Bridge] succeeded to load data block %s',
                         request.block_id)
            return tws2_pb.LoadDataBlockResponse(
                status=common_pb.Status(code=common_pb.STATUS_SUCCESS)
            )
        logging.info('[Bridge] failed to load data block %s', request.block_id)
        return tws2_pb.LoadDataBlockResponse(
            status=common_pb.Status(code=common_pb.STATUS_INVALID_DATA_BLOCK)
        )

    def new_iter_id(self):
        with self._condition:
            iter_id = self._next_iter_id
        return iter_id

    def start(self, iter_id):
        with self._condition:
            assert self._current_iter_id is None, \
                "[Bridge] Last iter not finished"
            self._current_iter_id = iter_id
            self._transmit(tws2_pb.TransmitRequest(
                start=tws2_pb.StartMessage(iter_id=iter_id)
            ))
            logging.debug("[Bridge] send start iter_id: %d", iter_id)

    def commit(self):
        with self._condition:
            assert self._current_iter_id is not None, "Not started yet"
            iter_id = self._current_iter_id
            self._current_iter_id = None
            self._next_iter_id += 1
            self._transmit(tws2_pb.TransmitRequest(
                commit=tws2_pb.CommitMessage(iter_id=iter_id)
            ))
            if iter_id in self._received_data:
                del self._received_data[iter_id]
            logging.debug("[Bridge] send commit iter_id: %d", iter_id)

    def register_data_block_handler(self, func):
        assert self._data_block_handler_fn is None, \
            "[Bridge] DataBlock handler already registered"
        self._data_block_handler_fn = func

    def load_data_block(self, count, block_id):
        req = tws2_pb.LoadDataBlockRequest(count=count, block_id=block_id)
        logging.debug("[Bridge] sending DataBlock with id %s", block_id)
        resp = self._client.LoadDataBlock(req)
        if resp.status.code == common_pb.STATUS_SUCCESS:
            logging.info('[Bridge] remote succeeded to load data block %s',
                block_id)
            return True
        logging.info('[Bridge] remoted failed to load data block %s. code: %d',
                     block_id, resp.status.code)
        return False

    def _send(self, iter_id, name, *, tensor=None, any_data=None):
        self._transmit(tws2_pb.TransmitRequest(
            data=tws2_pb.DataMessage(
                iter_id=iter_id,
                name=name,
                tensor=tensor,
                any_data=any_data,
            )))

    def send(self, iter_id, name, x):
        self._send(iter_id, name, tensor=tf.make_tensor_proto(x))

    def send_proto(self, iter_id, name, proto):
        any_proto = any_pb.Any()
        any_proto.Pack(proto)
        self._send(iter_id, name, any_data=any_proto)

    def send_op(self, name, x):
        def func(x):
            assert self._current_iter_id is not None, "[Bridge] not started"
            logging.debug("[Bridge] tensorflow run send_op,"
                          " iter_id: %d, name: %s",
                          self._current_iter_id, name)
            self.send(self._current_iter_id, name, x.numpy())

        out = tf.py_function(func=func, inp=[x], Tout=[], name='send_' + name)
        return out

    def _receive(self, iter_id, name):
        logging.debug('[Bridge] Data: waiting to receive iter_id: %d, name: %s',
            iter_id, name)
        start_time = time.time()
        with self._condition:
            while (iter_id not in self._received_data
                   or name not in self._received_data[iter_id]):
                if self._peer_commit_iter_id is not None \
                    and iter_id <= self._peer_commit_iter_id:
                    msg = "[Bridge] peer committed without sending {}" \
                        " for iter_id {}" \
                        " Please check model code".format(name, iter_id)
                    logging.fatal(msg)
                    raise RuntimeError(msg)
                if self._peer_terminated:
                    msg = "[Bridge] peer terminated without sending {}" \
                        " for iter_id {}".format(name, iter_id)
                    logging.fatal(msg)
                    raise RuntimeError(msg)
                duration = time.time() - start_time
                if duration >= self._waiting_alert_timeout:
                    logging.warning("[Bridge] Data: waiting to receive"
                        " iter_id: %d, name: %s timeout. duration: %f sec",
                        iter_id, name, duration)
                wait_timeout = self._waiting_alert_timeout - \
                    (duration % self._waiting_alert_timeout)
                self._condition.wait(wait_timeout)
            data = self._received_data[iter_id][name]

        duration = time.time() - start_time
        logging.debug("[Bridge] Data: received iter_id: %d, name: %s"
                      " after %f sec",
                      iter_id, name, duration)
        return data

    def receive(self, iter_id, name):
        return tf.make_ndarray(self._receive(iter_id, name).tensor)

    def receive_proto(self, iter_id, name):
        return self._receive(iter_id, name).any_data

    def receive_op(self, name, dtype):
        def func():
            assert self._current_iter_id is not None, "[Bridge] not started"
            logging.debug("[Bridge] tensorflow run receive_op,"
                          " iter_id: %d, name: %s",
                          self._current_iter_id, name)
            x = self.receive(self._current_iter_id, name)
            return tf.convert_to_tensor(x, dtype=dtype)

        return tf.py_function(func=func, inp=[], Tout=[dtype])[0]
