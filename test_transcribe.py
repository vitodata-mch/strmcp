"""Quick test: send a minimal WAV to /v1/transcribe and print the result."""
import struct
import io
import urllib.request

# Build a tiny valid WAV: 16kHz, mono, 16-bit, 1s silence
sample_rate = 16000
num_samples = sample_rate  # 1 second
data = b'\x00\x00' * num_samples

buf = io.BytesIO()
buf.write(b'RIFF')
buf.write(struct.pack('<I', 36 + len(data)))
buf.write(b'WAVE')
buf.write(b'fmt ')
buf.write(struct.pack('<I', 16))
buf.write(struct.pack('<H', 1))         # PCM format
buf.write(struct.pack('<H', 1))         # mono
buf.write(struct.pack('<I', sample_rate))
buf.write(struct.pack('<I', sample_rate * 2))  # byte rate
buf.write(struct.pack('<H', 2))         # block align
buf.write(struct.pack('<H', 16))        # bits per sample
buf.write(b'data')
buf.write(struct.pack('<I', len(data)))
buf.write(data)
wav_bytes = buf.getvalue()

boundary = b'----FormBoundaryXYZ123'
body = (
    b'--' + boundary + b'\r\n'
    b'Content-Disposition: form-data; name="file"; filename="test.wav"\r\n'
    b'Content-Type: audio/wav\r\n\r\n'
    + wav_bytes +
    b'\r\n--' + boundary + b'--\r\n'
)

req = urllib.request.Request(
    'http://localhost:8003/v1/transcribe',
    data=body,
    headers={'Content-Type': 'multipart/form-data; boundary=' + boundary.decode()}
)
try:
    r = urllib.request.urlopen(req, timeout=30)
    print('SUCCESS:', r.read().decode())
except urllib.error.HTTPError as e:
    print('HTTP Error:', e.code, e.read().decode())
except Exception as e:
    print('Error:', e)
