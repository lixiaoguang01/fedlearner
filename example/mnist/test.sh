#!/bin/bash
set -ex

BASE_DIR=`dirname $0`
cd $BASE_DIR

export CUDA_VISIBLE_DEVICES=""

rm -rf data model

python make_data.py

python leader.py --local-addr=localhost:50051     \
                 --peer-addr=localhost:50052      \
                 --data-path=data/leader          \
                 --checkpoint-path=log/checkpoint \
                 --save-checkpoint-steps=10       \
                 --summary-save-steps=10          \
                 --epoch-num=10 &

python follower.py --local-addr=localhost:50052     \
                   --peer-addr=localhost:50051      \
                   --data-path=data/follower/       \
                   --checkpoint-path=log/checkpoint \
                   --save-checkpoint-steps=10       \
                   --summary-save-steps=10          \
                   --epoch-num=10

wait

rm -rf data model
echo "test done"
