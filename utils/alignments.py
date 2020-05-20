import numpy as np
import tensorflow as tf

from model.transformer_utils import create_mel_padding_mask, create_encoder_padding_mask

logger = tf.get_logger()
logger.setLevel(40)


# for displaying only
def map_to_padded_length(inputs):
    duration, total_length, poss = inputs
    pos_zeros = tf.zeros(poss)
    markers = tf.ones(duration)
    try:
        padding = tf.zeros(total_length - (duration + poss))
    except Exception as e:
        print(e)
        print(total_length, duration, poss)
        return
    attention_weights = tf.concat([pos_zeros, markers, padding], axis=0)
    return attention_weights


# for displaying only
def duration_to_alignment_matrix(filled_durations):
    durs = tf.constant(filled_durations, dtype=tf.int32)[tf.newaxis, :]
    totlen = tf.math.reduce_sum(durs) * tf.ones(tf.shape(durs)[1], dtype=tf.int32)[tf.newaxis, :]
    pos = tf.math.cumsum(tf.concat([[0], durs[-1][:-1]], axis=0))[tf.newaxis, :]
    ctc = tf.transpose(tf.concat([durs, totlen, pos], axis=0))
    return tf.map_fn(map_to_padded_length, ctc, dtype=tf.float32).numpy()


def clean_attention(binary_attention, jump_threshold):
    phon_idx = 0
    clean_attn = np.zeros(binary_attention.shape)
    for i, av in enumerate(binary_attention):
        next_phon_idx = np.argmax(av)
        if abs(next_phon_idx - phon_idx) > jump_threshold:
            next_phon_idx = phon_idx
        phon_idx = next_phon_idx
        clean_attn[i, min(phon_idx, clean_attn.shape[0] - 1)] = 1
    return clean_attn


def weight_mask(attention_weights):
    """ Exponential loss mask based on distance from approximate diagonal"""
    max_m, max_n = attention_weights.shape
    I = np.tile(np.arange(max_n), (max_m, 1)) / max_n
    J = np.swapaxes(np.tile(np.arange(max_m), (max_n, 1)), 0, 1) / max_m
    return np.sqrt(np.square(I - J))


def fill_zeros(duration, take_from='next'):
    """ Fills zeros with one. Takes either from the next non-zero duration, or max."""
    for i in range(len(duration)):
        if i < (len(duration) - 1):
            if duration[i] == 0:
                if take_from == 'next':
                    next_avail = np.where(duration[i:] > 1)[0]
                    if len(next_avail) > 1:
                        next_avail = next_avail[0]
                elif take_from == 'max':
                    next_avail = np.argmax(duration[i:])
                if next_avail:
                    duration[i] = 1
                    duration[i + next_avail] -= 1
    return duration


def fix_attention_jumps(binary_attn, alignments_weights, binary_score):
    """ Scans for jumps in attention and attempts to fix. If score decreases, a collapse
        is likely so it tries to relax the jump size.
        Lower jumps size is more accurate, but more prone to collapse.
    """
    clean_scores = []
    clean_attns = []
    for jumpth in [2, 3, 4, 5]:
        cl_at = clean_attention(binary_attention=binary_attn, jump_threshold=jumpth)
        clean_attns.append(cl_at)
        sclean_score = np.sum(alignments_weights * cl_at)
        clean_scores.append(sclean_score)
    best_idx = np.argmin(clean_scores)
    best_score = clean_scores[best_idx]
    best_cleaned_attention = clean_attns[best_idx]
    while ((best_score - binary_score) > 2.) and (jumpth < 20):
        jumpth += 1
        best_cleaned_attention = clean_attention(binary_attention=binary_attn, jump_threshold=jumpth)
        best_score = np.sum(alignments_weights * best_cleaned_attention)
    return best_cleaned_attention


def binary_attention(attention_weights):
    attention_peak_per_phoneme = attention_weights.max(axis=1)
    binary_attn = (attention_weights.T == attention_peak_per_phoneme).astype(int).T
    binary_score = np.sum(attention_weights * binary_attn)
    return binary_attn, binary_score


def get_durations_from_alignment(batch_alignments, mels, phonemes, weighted=False, binary=False, fill_gaps=False,
                                 fix_jumps=False, fill_mode='max', plot_final_alignment=True):
    assert (binary is True) or (fix_jumps is False), 'Cannot fix jumps in non-binary attention.'
    mel_pad_mask = create_mel_padding_mask(mels)
    phon_pad_mask = create_encoder_padding_mask(phonemes)
    durations = []
    # remove start end token or vector
    unpad_mels = []
    unpad_phonemes = []
    final_alignment = []
    for i, al in enumerate(batch_alignments):
        mel_len = int(mel_pad_mask[i].shape[-1] - np.sum(mel_pad_mask[i]))
        phon_len = int(phon_pad_mask[i].shape[-1] - np.sum(phon_pad_mask[i]))
        unpad_alignments = al[:, 1:mel_len - 1, 1:phon_len - 1]  # first dim is heads
        unpad_mels.append(mels[i, 1:mel_len - 1, :])
        unpad_phonemes.append(phonemes[i, 1:phon_len - 1])
        alignments_weights = weight_mask(unpad_alignments[0])
        heads_scores = []
        scored_attention = []
        for _, attention_weights in enumerate(unpad_alignments):
            score = np.sum(alignments_weights * attention_weights)
            scored_attention.append(attention_weights / score)
            heads_scores.append(score)
        
        if weighted:
            ref_attention_weights = np.sum(scored_attention, axis=0)
        else:
            best_head = np.argmin(heads_scores)
            ref_attention_weights = unpad_alignments[best_head]
        
        if binary:  # pick max attention for each mel time-step
            binary_attn, binary_score = binary_attention(ref_attention_weights)
            if fix_jumps:
                binary_attn = fix_attention_jumps(
                    binary_attn=binary_attn,
                    alignments_weights=alignments_weights,
                    binary_score=binary_score)
            integer_durations = binary_attn.sum(axis=0)
        
        else:  # takes actual attention values and normalizes to mel_len
            attention_durations = np.sum(ref_attention_weights, axis=0)
            normalized_durations = attention_durations * ((mel_len - 2) / np.sum(attention_durations))
            integer_durations = np.round(normalized_durations)
            tot_duration = np.sum(integer_durations)
            duration_diff = tot_duration - (mel_len - 2)
            while duration_diff != 0:
                rounding_diff = integer_durations - normalized_durations
                if duration_diff > 0:  # duration is too long -> reduce highest (positive) rounding difference
                    max_error_idx = np.argmax(rounding_diff)
                    integer_durations[max_error_idx] -= 1
                elif duration_diff < 0:  # duration is too short -> increase lowest (negative) rounding difference
                    min_error_idx = np.argmin(rounding_diff)
                    integer_durations[min_error_idx] += 1
                tot_duration = np.sum(integer_durations)
                duration_diff = tot_duration - (mel_len - 2)
        
        if fill_gaps:  # fill zeros durations
            integer_durations = fill_zeros(integer_durations, take_from=fill_mode)
        
        assert np.sum(integer_durations) == mel_len - 2
        
        if plot_final_alignment:  # takes time
            new_alignment = duration_to_alignment_matrix(integer_durations)
            final_alignment.append(new_alignment)
        else:
            final_alignment = None
        durations.append(integer_durations)
    return durations, unpad_mels, unpad_phonemes, final_alignment