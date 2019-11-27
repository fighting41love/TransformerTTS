import librosa
import librosa.display
import numpy as np
import time
import tensorflow as tf

# TODO: we are now using transposed mel spectrograms. this needs to be fixed.
#       we are not using start and end vectors. are they needed?
# import IPython.display as ipd

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).absolute().parent
sys.path.append(SCRIPT_DIR.parent.as_posix())
from src.models import MelTransformer
from utils import display_mel

tf.random.set_seed(10)
np.random.seed(42)
# load audio
# get mel

# MAX_POW = 3000

import librosa
import numpy as np
import matplotlib.pyplot as plt
from librosa.display import specshow

power_exp = 1
n_fft = 1024
win_length = 1024
MEL_CHANNELS = 128
y, sr = librosa.load('/Users/cschaefe/datasets/de_DE/by_book/female/ramona_deininger/tom_sawyer/wavs/tom_sawyer_17_f000147.wav')
# ms = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=MEL_CHANNELS)
# y, ind = librosa.effects.trim(y, top_db=40, frame_length=2048, hop_length=512)
y, sr = librosa.load(librosa.util.example_audio_file())
ms = librosa.feature.melspectrogram(
    y=y, sr=sr, n_mels=MEL_CHANNELS, power=power_exp, n_fft=n_fft, win_length=win_length, hop_length=256, fmin=0, fmax=8000
)


# display_mel(ms, sr)
norm_ms = np.log(ms.clip(1e-5))
print((norm_ms.min(), norm_ms.max()))
print(norm_ms.shape)
# display_mel(norm_ms, sr)

params = {
    'num_layers': 1,
    'd_model': 40,
    'num_heads': 2,
    'dff': 30,
    'pe_input': ms.shape[1] + 1,
    'pe_target': ms.shape[1] + 1,
    'start_vec': np.ones(MEL_CHANNELS) * -1,
    'mel_channels': MEL_CHANNELS,
    'conv_filters': 64,
    'postnet_conv_layers': 5,
    'postnet_kernel_size': 5,
    'rate': 0.1,
}
melT = MelTransformer(**params)

losses = [tf.keras.losses.MeanSquaredError(), tf.keras.losses.BinaryCrossentropy()]
loss_coeffs = [0.5, 0.5]
optimizer = tf.keras.optimizers.Adam(1e-4, beta_1=0.9, beta_2=0.98, epsilon=1e-9)
melT.compile(loss=losses, loss_weights=loss_coeffs, optimizer=optimizer)


train_samples = []
start_vec = np.ones((1, MEL_CHANNELS)) * -1
end_vec = np.ones((1, MEL_CHANNELS)) * -2
stop = False
cursor = 0
while cursor < (norm_ms.shape[0] - 100):
    size = np.random.randint(50, 100)
    sample = norm_ms[cursor : cursor + size, :]
    sample = np.concatenate([start_vec, sample, end_vec])
    stop_probs = np.zeros(size + 2)
    stop_probs[-1] = 1
    train_samples.append((sample, stop_probs))
    cursor += size

train_gen = lambda: (mel for mel in train_samples)
train_dataset = tf.data.Dataset.from_generator(train_gen, output_types=(tf.float64, tf.int64))
train_dataset = train_dataset.padded_batch(2, padded_shapes=([-1, MEL_CHANNELS], [-1]))

EPOCHS = 10
losses = []
for epoch in range(EPOCHS):
    start = time.time()
    for i, (mel, stop) in enumerate(train_dataset):
        gradients, loss, tar_real, predictions, stop_pred = melT.train_step(mel, mel, stop)
        losses.append(loss)
        print('loss:', loss.numpy())

    print('Epoch {} took {} secs\n'.format(epoch, time.time() - start))


out = melT.predict(norm_ms, MAX_LENGTH=100)
print(out['output'].shape)
melT.save_weights('melT_weights.hdf5')

stft = librosa.feature.inverse.mel_to_stft(S, sr=22050, n_fft=n_fft, power=power_exp, fmin=0, fmax=8000)
wav = librosa.feature.inverse.griffinlim(stft, n_iter=60, hop_length=256, win_length=win_length)
"""
wav = librosa.feature.inverse.mel_to_audio(S,
                                           sr=sr,
                                           hop_length=256,
                                           n_fft=n_fft,
                                           win_length=win_length,
                                           power=power_exp)
"""
librosa.output.write_wav(f'/tmp/sample_{num_mels}.wav', wav, sr)