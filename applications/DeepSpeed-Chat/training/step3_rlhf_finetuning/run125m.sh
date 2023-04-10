#!/bin/bash

OUTPUT=$1
ACTOR_MODEL_PATH=$2
ACTOR_ZERO_STAGE="--actor_zero_stage $3"
CRITIC_MODEL_PATH=$4
CRITIC_ZERO_STAGE="--critic_zero_stage $5"

Num_Padding_at_Beginning=1 # this is model related

Actor_Lr=5e-4
Critic_Lr=5e-6

mkdir -p $OUTPUT

deepspeed --include localhost:1 main.py \
   --data_path Dahoas/rm-static \
   --data_split 2,4,4 \
   --actor_model_name_or_path $ACTOR_MODEL_PATH \
   --critic_model_name_or_path $CRITIC_MODEL_PATH \
   --num_padding_at_beginning 1 \
   --per_device_train_batch_size 16 \
   --per_device_mini_train_batch_size 16 \
   --generation_batch_numbers 1 \
   --ppo_epochs 1 \
   --max_answer_seq_len 256 \
   --max_prompt_seq_len 256 \
   --ppo_epochs 1 \
   --actor_learning_rate ${Actor_Lr} \
   --critic_learning_rate ${Critic_Lr} \
   --actor_weight_decay 0.1 \
   --critic_weight_decay 0.1 \
   --num_train_epochs 1 \
   --lr_scheduler_type cosine \
   --gradient_accumulation_steps 16 \
   --num_warmup_steps 100 \
   --deepspeed --seed 1234 \
   ${ACTOR_ZERO_STAGE} \
   ${CRITIC_ZERO_STAGE} ${OFFLOAD}\
   --actor_lora_dim 128 \
   --actor_gradient_checkpointing \
   --critic_gradient_checkpointing \
   --enable_hybrid_engine \
   --output_dir $OUTPUT \
    &> $OUTPUT/training.log &
