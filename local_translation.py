"""English-to-Russian translation with the published Argos model."""
import hashlib
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile

import ctranslate2
import requests
import sentencepiece

MODEL_NAME = "translate-en_ru-1_9"
MODEL_DIR = Path(__file__).resolve().parent / ".models" / MODEL_NAME
MODEL_URL = f"https://argos-net.com/v1/{MODEL_NAME}.argosmodel"
MODEL_SHA256 = "591d743ae103752b88ffc38785c50421320f4eff93c8967e0d3d2e14d4e27811"
MODEL_FILES = ("sentencepiece.model", "model/config.json", "model/model.bin",
               "model/shared_vocabulary.json")


def install():
    if all((MODEL_DIR / name).is_file() for name in MODEL_FILES):
        return
    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(dir=MODEL_DIR.parent) as temporary:
        archive_path = Path(temporary) / "model.zip"
        with requests.get(MODEL_URL, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            with archive_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise RuntimeError("Контрольная сумма модели Argos не совпала")
        with ZipFile(archive_path) as archive:
            archive.extractall(temporary, [f"{MODEL_NAME}/{name}" for name in MODEL_FILES])
        (Path(temporary) / MODEL_NAME).replace(MODEL_DIR)


@lru_cache(maxsize=1)
def load_model():
    processor = sentencepiece.SentencePieceProcessor(model_file=str(MODEL_DIR / "sentencepiece.model"))
    translator = ctranslate2.Translator(str(MODEL_DIR / "model"), device="cpu",
                                       compute_type="int8", intra_threads=2)
    return processor, translator


def translate_english(text):
    processor, translator = load_model()
    paragraphs = []
    for paragraph in text.splitlines():
        segments = []
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            tokens = processor.encode(sentence, out_type=str)
            segments.extend(tokens[i:i + 160] for i in range(0, len(tokens), 160))
        if segments:
            results = translator.translate_batch(segments, batch_type="tokens", max_batch_size=480,
                                                 beam_size=4, max_input_length=160, max_decoding_length=320)
            paragraphs.append(" ".join(processor.decode(result.hypotheses[0]) for result in results))
    return "\n\n".join(paragraphs)


if __name__ == "__main__":
    install()
