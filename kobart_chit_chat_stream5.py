import streamlit as st
from streamlit_chat import message

import argparse
import logging
import os

import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
from pytorch_lightning import loggers as pl_loggers
from torch.utils.data import DataLoader, Dataset
from transformers import (BartForConditionalGeneration,
                          PreTrainedTokenizerFast)
from transformers.optimization import AdamW, get_cosine_schedule_with_warmup


parser = argparse.ArgumentParser(description='KoBART Chit-Chat')


parser.add_argument('--checkpoint_path',
                    type=str,
                    help='checkpoint path')

parser.add_argument('--chat',
                    action='store_true',
                    default=True,
                    help='response generation on given user input')

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ArgsBase():
    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = argparse.ArgumentParser(
            parents=[parent_parser], add_help=False)
        # parser.add_argument('--train_file',
        #                     type=str,
        #                     default='Chatbot_data/train.csv',
        #                     help='train file')

        # parser.add_argument('--test_file',
        #                     type=str,
        #                     default='Chatbot_data/test.csv',
        #                     help='test file')

        parser.add_argument('--tokenizer_path',
                            type=str,
                            default='emji_tokenizer',
                            help='tokenizer')
        parser.add_argument('--batch_size',
                            type=int,
                            default=14,
                            help='')
        parser.add_argument('--max_seq_len',
                            type=int,
                            default=36,
                            help='max seq len')
        return parser


class ChatDataset(Dataset):
    def __init__(self, filepath, tok_vocab, max_seq_len=128) -> None:
        self.filepath = filepath
        self.data = pd.read_csv(self.filepath)
        self.bos_token = '<s>'
        self.eos_token = '</s>'
        self.max_seq_len = max_seq_len
        self.tokenizer = PreTrainedTokenizerFast(
            tokenizer_file=tok_vocab,
            bos_token=self.bos_token, eos_token=self.eos_token, unk_token='<unk>', pad_token='<pad>', mask_token='<mask>')

    def __len__(self):
        return len(self.data)

    def make_input_id_mask(self, tokens, index):
        input_id = self.tokenizer.convert_tokens_to_ids(tokens)
        attention_mask = [1] * len(input_id)
        if len(input_id) < self.max_seq_len:
            while len(input_id) < self.max_seq_len:
                input_id += [self.tokenizer.pad_token_id]
                attention_mask += [0]
        else:
            # logging.warning(f'exceed max_seq_len for given article : {index}')
            input_id = input_id[:self.max_seq_len - 1] + [
                self.tokenizer.eos_token_id]
            attention_mask = attention_mask[:self.max_seq_len]
        return input_id, attention_mask

    def __getitem__(self, index):
        record = self.data.iloc[index]
        q, a = record['Q'], record['A']
        q_tokens = [self.bos_token] + \
            self.tokenizer.tokenize(q) + [self.eos_token]
        a_tokens = [self.bos_token] + \
            self.tokenizer.tokenize(a) + [self.eos_token]
        encoder_input_id, encoder_attention_mask = self.make_input_id_mask(
            q_tokens, index)
        decoder_input_id, decoder_attention_mask = self.make_input_id_mask(
            a_tokens, index)
        labels = self.tokenizer.convert_tokens_to_ids(
            a_tokens[1:(self.max_seq_len + 1)])
        if len(labels) < self.max_seq_len:
            while len(labels) < self.max_seq_len:
                # for cross entropy loss masking
                labels += [-100]
        return {'input_ids': np.array(encoder_input_id, dtype=np.int_),
                'attention_mask': np.array(encoder_attention_mask, dtype=np.float_),
                'decoder_input_ids': np.array(decoder_input_id, dtype=np.int_),
                'decoder_attention_mask': np.array(decoder_attention_mask, dtype=np.float_),
                'labels': np.array(labels, dtype=np.int_)}


class ChatDataModule(pl.LightningDataModule):
    def __init__(self, train_file,
                 test_file, tok_vocab,
                 max_seq_len=128,
                 batch_size=32,
                 num_workers=5):
        super().__init__()
        self.batch_size = batch_size
        self.max_seq_len = max_seq_len
        self.train_file_path = train_file
        self.test_file_path = test_file
        self.tok_vocab = tok_vocab
        self.num_workers = num_workers

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = argparse.ArgumentParser(
            parents=[parent_parser], add_help=False)
        parser.add_argument('--num_workers',
                            type=int,
                            default=5,
                            help='num of worker for dataloader')
        return parser

    # OPTIONAL, called for every GPU/machine (assigning state is OK)
    def setup(self, stage):
        # split dataset
        self.train = ChatDataset(self.train_file_path,
                                 self.tok_vocab,
                                 self.max_seq_len)
        self.test = ChatDataset(self.test_file_path,
                                self.tok_vocab,
                                self.max_seq_len)

    def train_dataloader(self):
        train = DataLoader(self.train,
                           batch_size=self.batch_size,
                           num_workers=self.num_workers, shuffle=True)
        return train

    def val_dataloader(self):
        val = DataLoader(self.test,
                         batch_size=self.batch_size,
                         num_workers=self.num_workers, shuffle=False)
        return val

    def test_dataloader(self):
        test = DataLoader(self.test,
                          batch_size=self.batch_size,
                          num_workers=self.num_workers, shuffle=False)
        return test


class Base(pl.LightningModule):
    def __init__(self, hparams, **kwargs) -> None:
        super(Base, self).__init__()
        self.save_hyperparameters()
        self.hparams = hparams

    @staticmethod
    def add_model_specific_args(parent_parser):
        # add model specific args
        parser = argparse.ArgumentParser(
            parents=[parent_parser], add_help=False)

        parser.add_argument('--batch-size',
                            type=int,
                            default=14,
                            help='batch size for training (default: 96)')

        parser.add_argument('--lr',
                            type=float,
                            default=5e-5,
                            help='The initial learning rate')

        parser.add_argument('--warmup_ratio',
                            type=float,
                            default=0.1,
                            help='warmup ratio')

        parser.add_argument('--model_path',
                            type=str,
                            default='kobart_from_pretrained',
                            help='kobart model path')
        return parser

    def configure_optimizers(self):
        # Prepare optimizer
        param_optimizer = list(self.model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(
                nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(
                nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=self.hparams.lr, correct_bias=False)
        # warm up lr
        num_workers = (1 if 1 is not None else 1) * (self.hparams.num_nodes if self.hparams.num_nodes is not None else 1)
        data_len = len(self.train_dataloader().dataset)
        logging.info(f'number of workers {num_workers}, data length {data_len}')
        num_train_steps = int(data_len / (self.hparams.batch_size * num_workers) * 3)
        logging.info(f'num_train_steps : {num_train_steps}')
        num_warmup_steps = int(num_train_steps * self.hparams.warmup_ratio)
        logging.info(f'num_warmup_steps : {num_warmup_steps}')
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=num_warmup_steps, num_training_steps=num_train_steps)
        lr_scheduler = {'scheduler': scheduler, 
                        'monitor': 'loss', 'interval': 'step',
                        'frequency': 1}
        return [optimizer], [lr_scheduler]


class KoBARTConditionalGeneration(Base):
    def __init__(self, hparams, **kwargs):
        super(KoBARTConditionalGeneration, self).__init__(hparams, **kwargs)
        self.model = BartForConditionalGeneration.from_pretrained(self.hparams.model_path)
        self.model.train()
        self.bos_token = '<s>'
        self.eos_token = '</s>'
        self.tokenizer = PreTrainedTokenizerFast(
            tokenizer_file=os.path.join(self.hparams.tokenizer_path, 'model.json'),
            bos_token=self.bos_token, eos_token=self.eos_token, unk_token='<unk>', pad_token='<pad>', mask_token='<mask>')

    def forward(self, inputs):
        return self.model(input_ids=inputs['input_ids'],
                          attention_mask=inputs['attention_mask'],
                          decoder_input_ids=inputs['decoder_input_ids'],
                          decoder_attention_mask=inputs['decoder_attention_mask'],
                          labels=inputs['labels'], return_dict=True)

    def training_step(self, batch, batch_idx):
        outs = self(batch)
        loss = outs.loss
        self.log('train_loss', loss, prog_bar=True, on_step=True, on_epoch=True)
        return loss

    def validation_step(self, batch, batch_idx):
        outs = self(batch)
        loss = outs['loss']
        self.log('val_loss', loss, on_step=True, on_epoch=True, prog_bar=True)


    def chat(self, text):
        input_ids =  [self.tokenizer.bos_token_id] + self.tokenizer.encode(text) + [self.tokenizer.eos_token_id]
        res_ids = self.model.generate(torch.tensor([input_ids]),
                                            max_length=self.hparams.max_seq_len,
                                            num_beams=5,
                                            eos_token_id=self.tokenizer.eos_token_id,
                                            bad_words_ids=[[self.tokenizer.unk_token_id]])        
        a = self.tokenizer.batch_decode(res_ids.tolist())[0]
        return a.replace('<s>', '').replace('</s>', '').replace('<usr>', '')


if __name__ == '__main__':
    parser = Base.add_model_specific_args(parser)
    parser = ArgsBase.add_model_specific_args(parser)
    parser = ChatDataModule.add_model_specific_args(parser)
    parser = pl.Trainer.add_argparse_args(parser)
    args = parser.parse_args()
    logging.info(args)


    @st.cache(allow_output_mutation=True) # 캐시. True로 해줘야 새 인풋값에 대해 학습해서 text generation함
    def cached_model():
        chk_path = "epoch03.ckpt" # 모델 패스
        model2 = KoBARTConditionalGeneration(args).load_from_checkpoint(chk_path) # 모델 로드
        return model2
    
    model = cached_model() # model2

    st.header('Chat Bot')
    st.markdown('캐시에 모델까지 올린 버전')

    if 'past' not in st.session_state: # 내 입력채팅값 저장할 리스트
        st.session_state['past'] = [] 

    if 'generated' not in st.session_state: # 챗봇채팅값 저장할 리스트
        st.session_state['generated'] = []
    

    placeholder = st.empty() # 채팅 입력창을 아래위치로 내려주기위해 빈 부분을 하나 만듬
    
    with st.form('form', clear_on_submit=True): # 채팅 입력창 생성
        user_input = st.text_input('당신: ', '') # 입력부분
        submitted = st.form_submit_button('전송') # 전송 버튼
    
    
    if submitted and user_input:
        user_input1 = user_input.strip() # 채팅 입력값 및 여백제거
        chatbot_output1 = model.chat(user_input1).strip() # text generation된 값 및 여백 제거
        st.session_state.past.append(user_input1) # 입력값을 past 에 append -> 채팅 로그값 저장을 위해
        st.session_state.generated.append(chatbot_output1)
    
    with placeholder.container(): # 리스트에 append된 채팅입력과 로봇출력을 리스트에서 꺼내서 메세지로 출력
        for i in range(len(st.session_state['past'])):
            message(st.session_state['past'][i], is_user=True, key=str(i) + '_user')
            if len(st.session_state['generated']) > i:
                message(st.session_state['generated'][i], key=str(i) + '_bot')