# Third-Party Licenses

DSH-VHCU itself is released under the MIT License (see [LICENSE](LICENSE)).
It depends on the following third-party components. Their licenses apply to
those components, not to VHCU's own source code.

## Runtime components

| Component | Purpose in VHCU | License | Upstream |
|---|---|---|---|
| **faster-whisper** | Local/offline speech recognition engine | MIT | <https://github.com/SYSTRAN/faster-whisper> |
| **OpenAI Whisper `base` model** | Offline ASR model used by the Hear feature | MIT | <https://github.com/openai/whisper> · <https://huggingface.co/Systran/faster-whisper-base> |
| **mss** (`python-mss`) | Screen capture (BitBlt / multiple screenshots) | MIT | <https://github.com/BoboTiG/python-mss> |
| **SoundCard** | WASAPI loopback capture for Hear | BSD-3-Clause | <https://github.com/bastibe/SoundCard> |
| **webrtcvad-wheels** | Voice activity detection (VAD) | MIT | <https://github.com/daanzu/py-webrtcvad-wheels> |
| **NumPy** | Audio/array math | BSD-3-Clause | <https://numpy.org/> |
| **uiohook-napi** | Optional global keyboard/mouse recording | MIT | <https://github.com/SnosMe/uiohook-napi> |

Notes:

- `faster-whisper` is licensed under the MIT License. The `base` model used by
  the offline Hear pack is derived from OpenAI Whisper and is distributed under
  the MIT License.
- `webrtcvad-wheels` wraps the WebRTC Voice Activity Detector. The Python
  package is MIT-licensed; the upstream WebRTC project retains its own notices.
- `mss` and `SoundCard` are Python runtime dependencies listed in
  [requirements.txt](requirements.txt).
- Transitive dependencies (for example CTranslate2, tokenizers,
  huggingface-hub, onnxruntime, cffi and NumPy's build dependencies) keep their
  own licenses. See the installed package metadata for the exact versions in a
  given environment.

## Copyright notices

- faster-whisper: Copyright (c) SYSTRAN
- OpenAI Whisper: Copyright (c) OpenAI
- mss: Copyright (c) 2013–2026 Mickaël "Tiger-222" Schoentgen
- SoundCard: Copyright (c) 2016 Bastian Bechtold
- webrtcvad-wheels: Copyright (c) David Zurow; WebRTC VAD Copyright (c) Google
- NumPy: Copyright (c) NumPy Developers
- uiohook-napi: Copyright (c) 2020 Alexander Drozdov

## MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## BSD 3-Clause License

Copyright (c) 2016, Bastian Bechtold
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

---

If you redistribute VHCU with a modified dependency set, update this file and
include the corresponding license texts. The upstream links above are the
authoritative source for each project's current license.
