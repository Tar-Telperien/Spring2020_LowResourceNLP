'''
all model
'''
import math
from collections import namedtuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Distribution

from dataloader_clean import BOS_IDX, EOS_IDX, PAD_IDX, STEP_IDX

EPSILON = 1e-7
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class Identity(nn.Module):
    def forward(self, x):
        return x

class Transducer(nn.Module):
    '''
    seq2seq with soft attention baseline
    '''

    def __init__(self, *, src_vocab_size, trg_vocab_size, embed_dim,
                 src_hid_size, src_nb_layers, trg_hid_size, trg_nb_layers,
                 dropout_p, src_c2i, trg_c2i, attr_c2i, **kwargs):
        '''
        init
        '''
        super().__init__()
        self.src_vocab_size = src_vocab_size
        self.trg_vocab_size = trg_vocab_size
        self.embed_dim = embed_dim
        self.src_hid_size = src_hid_size
        self.src_nb_layers = src_nb_layers
        self.trg_hid_size = trg_hid_size
        self.trg_nb_layers = trg_nb_layers
        self.dropout_p = dropout_p
        self.src_c2i, self.trg_c2i, self.attr_c2i = src_c2i, trg_c2i, attr_c2i
        self.src_embed = nn.Embedding(
            src_vocab_size, embed_dim, padding_idx=PAD_IDX)
        self.trg_embed = nn.Embedding(
            trg_vocab_size, embed_dim, padding_idx=PAD_IDX)
        self.enc_rnn = nn.LSTM(
            embed_dim,
            src_hid_size,
            src_nb_layers,
            bidirectional=True,
            dropout=dropout_p)
        self.dec_rnn = StackedLSTM(embed_dim, trg_hid_size, trg_nb_layers,
                                   dropout_p)
        self.out_dim = trg_hid_size + src_hid_size * 2
        self.scale_enc_hs = nn.Linear(src_hid_size * 2, trg_hid_size)
        self.attn = Attention()
        self.linear_out = nn.Linear(self.out_dim, self.out_dim)
        self.final_out = nn.Linear(self.out_dim, trg_vocab_size)
        self.dropout = nn.Dropout(dropout_p)

    def encode(self, src_batch):
        '''
        encoder
        '''
        enc_hs, _ = self.enc_rnn(self.dropout(self.src_embed(src_batch)))
        scale_enc_hs = self.scale_enc_hs(enc_hs)
        return enc_hs, scale_enc_hs

    def decode_step(self, enc_hs, enc_mask, input_, hidden):
        '''
        decode step
        '''
        h_t, hidden = self.dec_rnn(input_, hidden)
        ctx, attn = self.attn(h_t, enc_hs, enc_mask)
        # Concatenate the ht and ctx
        # weight_hs: batch x (hs_dim + ht_dim)
        ctx = torch.cat((ctx, h_t), dim=1)
        # ctx: batch x out_dim
        ctx = self.linear_out(ctx)
        ctx = torch.tanh(ctx)
        word_logprob = F.log_softmax(self.final_out(ctx), dim=-1)
        return word_logprob, hidden, attn

    def decode(self, enc_hs, enc_mask, trg_batch):
        '''
        enc_hs: tuple(enc_hs, scale_enc_hs)
        '''
        trg_seq_len = trg_batch.size(0)
        trg_bat_siz = trg_batch.size(1)
        trg_embed = self.dropout(self.trg_embed(trg_batch))
        output = []
        hidden = self.dec_rnn.get_init_hx(trg_bat_siz)
        for idx in range(trg_seq_len - 1):
            input_ = trg_embed[idx, :]
            word_logprob, hidden, _ = self.decode_step(enc_hs, enc_mask,
                                                       input_, hidden)
            output += [word_logprob]
        return torch.stack(output)

    def forward(self, src_batch, src_mask, trg_batch):
        '''
        only for training
        '''
        # trg_seq_len, batch_size = trg_batch.size()
        enc_hs = self.encode(src_batch)
        # output: [trg_seq_len-1, batch_size, vocab_siz]
        output = self.decode(enc_hs, src_mask, trg_batch)
        return output

    def count_nb_params(self):
        model_parameters = filter(lambda p: p.requires_grad, self.parameters())
        params = sum([np.prod(p.size()) for p in model_parameters])
        return params

    def loss(self, predict, target):
        '''
        compute loss
        '''
        return F.nll_loss(
            predict.view(-1, self.trg_vocab_size),
            target.view(-1),
            ignore_index=PAD_IDX)

    def get_loss(self, data):
        src, src_mask, trg, _ = data
        out = self.forward(src, src_mask, trg)
        loss = self.loss(out, trg[1:])
        return loss


HMMState = namedtuple('HMMState', 'init trans emiss')


class HMM(object):
    def __init__(self, nb_states, nb_tokens, initial, transition, emission):
        assert isinstance(initial, torch.Tensor)
        assert isinstance(transition, torch.Tensor)
        assert isinstance(emission, torch.Tensor)
        assert initial.shape[-1] == nb_states
        assert transition.shape[-2:] == (nb_states, nb_states)
        assert emission.shape[-2:] == (nb_states, nb_tokens)
        self.ns = nb_states
        self.V = nb_tokens
        self.initial = initial
        self.transition = transition
        self.emission = emission

    def emiss(self, T, idx, ignore_index=None):
        assert len(idx.shape) == 1
        bs = idx.shape[0]
        idx = idx.view(-1, 1).expand(bs, self.ns).unsqueeze(-1)
        emiss = torch.gather(self.emission[T], -1, idx).view(bs, 1, self.ns)
        if ignore_index is None:
            return emiss
        else:
            idx = idx.view(bs, 1, self.ns)
            mask = (idx != ignore_index).float()
            return emiss * mask

    def p_x(self, seq, ignore_index=None):
        T, bs = seq.shape
        assert self.initial.shape == (bs, 1, self.ns)
        assert self.transition.shape == (T - 1, bs, self.ns, self.ns)
        assert self.emission.shape == (T, bs, self.ns, self.V)
        # fwd = pi * b[:, O[0]]
        # fwd = self.initial * self.emiss(0, seq[0])
        fwd = self.initial + self.emiss(0, seq[0], ignore_index=ignore_index)
        #induction:
        for t in range(T - 1):
            # fwd[t + 1] = np.dot(fwd[t], a) * b[:, O[t + 1]]
            # fwd = torch.bmm(fwd, self.transition[t]) * self.emiss(
            #     t + 1, seq[t + 1])
            fwd = fwd + self.transition[t].transpose(1, 2)
            fwd = fwd.logsumexp(dim=-1, keepdim=True).transpose(1, 2)
            fwd = fwd + self.emiss(
                t + 1, seq[t + 1], ignore_index=ignore_index)
        return fwd


class HMMTransducer(Transducer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        del self.attn

    def loss(self, predict, target):
        assert isinstance(predict, HMMState)
        seq_len = target.shape[0]
        hmm = HMM(predict.init.shape[-1], self.trg_vocab_size, predict.init,
                  predict.trans, predict.emiss)
        loss = hmm.p_x(target, ignore_index=PAD_IDX)
        return -torch.logsumexp(loss, dim=-1).mean() / seq_len

    def decode(self, enc_hs, enc_mask, trg_batch):
        trg_seq_len = trg_batch.size(0)
        trg_bat_siz = trg_batch.size(1)
        trg_embed = self.dropout(self.trg_embed(trg_batch))
        hidden = self.dec_rnn.get_init_hx(trg_bat_siz)

        initial, transition, emission = None, list(), list()
        for idx in range(trg_seq_len - 1):
            input_ = trg_embed[idx, :]
            trans, emiss, hidden = self.decode_step(enc_hs, enc_mask, input_,
                                                    hidden)
            if idx == 0:
                initial = trans[:, 0].unsqueeze(1)
                emission += [emiss]
            else:
                transition += [trans]
                emission += [emiss]
        transition = torch.stack(transition)
        emission = torch.stack(emission)
        return HMMState(initial, transition, emission)

    def decode_step(self, enc_hs, enc_mask, input_, hidden):
        src_seq_len, bat_siz = enc_mask.shape
        h_t, hidden = self.dec_rnn(input_, hidden)

        # Concatenate the ht and hs
        # ctx_*: batch x seq_len x (trg_hid_siz+src_hid_size*2)
        ctx_curr = torch.cat(
            (h_t.unsqueeze(1).expand(-1, src_seq_len, -1), enc_hs[0].transpose(
                0, 1)),
            dim=2)

        hs_ = enc_hs[1].transpose(0, 1)
        h_t = h_t.unsqueeze(2)
        score = torch.bmm(hs_, h_t).squeeze(2)
        trans = F.softmax(score, dim=-1) * enc_mask.transpose(0, 1) + EPSILON
        trans = trans / trans.sum(-1, keepdim=True)
        trans = trans.unsqueeze(1).log()
        trans = trans.expand(bat_siz, src_seq_len, src_seq_len)

        ctx = torch.tanh(self.linear_out(ctx_curr))
        # emiss: batch x seq_len x nb_vocab
        emiss = F.log_softmax(self.final_out(ctx), dim=-1)

        return trans, emiss, hidden


class MonoHMMTransducer(HMMTransducer):
    def decode_step(self, enc_hs, enc_mask, input_, hidden):
        trans, emiss, hidden = super().decode_step(enc_hs, enc_mask, input_,
                                                   hidden)
        trans_mask = torch.ones_like(trans[0]).triu().unsqueeze(0)
        trans_mask = (trans_mask - 1) * -np.log(EPSILON)
        trans = trans + trans_mask
        trans = trans - trans.logsumexp(-1, keepdim=True)
        return trans, emiss, hidden


class HardMonoTransducer(Transducer):
    def __init__(self, *, nb_attr, **kwargs):
        super().__init__(**kwargs)
        self.nb_attr = nb_attr + 1 if nb_attr > 0 else 0
        # StackedLSTM(embed_dim, trg_hid_size, trg_nb_layers, dropout_p)
        hs = self.cal_hs(
            layer=self.trg_nb_layers,
            ed=self.embed_dim,
            od=self.out_dim,
            vs=self.trg_vocab_size,
            hs=self.src_hid_size,
            ht=self.trg_hid_size)
        if self.nb_attr > 0:
            self.merge_attr = nn.Linear(self.embed_dim * self.nb_attr,
                                        self.embed_dim)
            self.dec_rnn = StackedLSTM(
                self.embed_dim * 2 + self.src_hid_size * 2, hs,
                self.trg_nb_layers, self.dropout_p)
        else:
            self.dec_rnn = StackedLSTM(self.embed_dim + self.src_hid_size * 2,
                                       hs, self.trg_nb_layers, self.dropout_p)
        # nn.Linear(self.out_dim, trg_vocab_size)
        self.final_out = nn.Linear(hs, self.trg_vocab_size)
        del self.scale_enc_hs  # nn.Linear(src_hid_size * 2, trg_hid_size)
        del self.attn
        del self.linear_out  # nn.Linear(self.out_dim, self.out_dim)

    def cal_hs(self, *, layer, ed, od, vs, hs, ht):
        b = ed + 2 * hs + 2 + vs / 4
        if self.nb_attr > 0: b += ed
        c = ed * ed * self.nb_attr + ed - od * (od + vs + 1) - \
            ht * (2 * hs + 4 * ht + 4 * ed + 1 + 4 * 2)
        c /= 4
        if layer > 1:
            c -= (layer - 1) * (2 * ht**2 + 2 * ht)
            b += (layer - 1) * 2
            b /= (layer * 2 - 1)
            c /= (layer * 2 - 1)
        return round((math.sqrt(b * b - 4 * c) - b) / 2)

    def encode(self, src_batch):
        '''
        encoder
        '''
        if self.nb_attr > 0:
            assert isinstance(src_batch, tuple) and len(src_batch) == 2
            src, attr = src_batch
            bs = src.shape[1]
            enc_hs, _ = self.enc_rnn(self.dropout(self.src_embed(src)))
            enc_attr = F.relu(
                self.merge_attr(self.src_embed(attr).view(bs, -1)))
            return enc_hs, enc_attr
        else:
            enc_hs, _ = self.enc_rnn(self.dropout(self.src_embed(src_batch)))
            return enc_hs, None

    def decode_step(self, enc_hs, enc_mask, input_, hidden, attn_pos):
        '''
        decode step
        '''
        source, attr = enc_hs
        bs = source.shape[1]
        if isinstance(attn_pos, int):
            assert bs == 1
            ctx = source[attn_pos]
        else:
            ctx = fancy_gather(source, attn_pos).squeeze(0)
        if attr is None:
            input_ = torch.cat((input_, ctx), dim=1)
        else:
            input_ = torch.cat((input_, attr, ctx), dim=1)
        h_t, hidden = self.dec_rnn(input_, hidden)
        word_logprob = F.log_softmax(self.final_out(h_t), dim=-1)
        return word_logprob, hidden, None

    def decode(self, enc_hs, enc_mask, trg_batch):
        '''
        enc_hs: tuple(enc_hs, enc_attr)
        '''
        trg_seq_len = trg_batch.size(0)
        trg_bat_siz = trg_batch.size(1)
        attn_pos = torch.zeros((1, trg_bat_siz),
                               dtype=torch.long,
                               device=DEVICE)
        trg_embed = self.dropout(self.trg_embed(trg_batch))
        output = []
        hidden = self.dec_rnn.get_init_hx(trg_bat_siz)
        for idx in range(trg_seq_len - 1):
            for j in range(trg_bat_siz):
                if trg_batch[idx, j] == STEP_IDX:
                    attn_pos[0, j] += 1
            input_ = trg_embed[idx, :]
            word_logprob, hidden, _ = self.decode_step(
                enc_hs, enc_mask, input_, hidden, attn_pos)
            output += [word_logprob]
        return torch.stack(output)

class HardAttnTransducer(Transducer):
    def decode_step(self, enc_hs, enc_mask, input_, hidden):
        '''
        enc_hs: tuple(enc_hs, scale_enc_hs)
        '''
        src_seq_len = enc_hs[0].size(0)
        h_t, hidden = self.dec_rnn(input_, hidden)

        # ht: batch x trg_hid_dim
        # enc_hs: seq_len x batch x src_hid_dim*2
        # attns: batch x 1 x seq_len
        _, attns = self.attn(h_t, enc_hs, enc_mask, weighted_ctx=False)

        # Concatenate the ht and hs
        # ctx: batch x seq_len x (trg_hid_siz+src_hid_size*2)
        ctx = torch.cat(
            (h_t.unsqueeze(1).expand(-1, src_seq_len, -1), enc_hs[0].transpose(
                0, 1)),
            dim=2)
        # ctx: batch x seq_len x out_dim
        ctx = self.linear_out(ctx)
        ctx = torch.tanh(ctx)

        # word_prob: batch x seq_len x nb_vocab
        word_prob = F.softmax(self.final_out(ctx), dim=-1)
        # word_prob: batch x nb_vocab
        word_prob = torch.bmm(attns, word_prob).squeeze(1)
        return torch.log(word_prob), hidden, attns


def fancy_gather(value, index):
    assert value.size(1) == index.size(1)
    split = zip(value.split(1, dim=1), index.split(1, dim=1))
    return torch.cat([v[i.view(-1)] for v, i in split], dim=1)


class Categorical(Distribution):
    def __init__(self, probs):
        assert probs.dim() == 2
        self.nb_prob, self.nb_choice = probs.size()
        self.probs = probs
        self.probs_t = probs.t()

    def sample_n(self, n):
        return torch.multinomial(self.probs, n, True).t()

    def log_prob(self, value):
        return (fancy_gather(self.probs_t, value) + EPSILON).log()

def dummy_mask(seq):
    '''
    create dummy mask (all 1)
    '''
    if isinstance(seq, tuple):
        seq = seq[0]
    assert len(seq.size()) == 1 or (len(seq.size()) == 2 and seq.size(1) == 1)
    return torch.ones_like(seq, dtype=torch.float)