import io
import json
import zipfile

import pytest

import datasync


def write(path, name, data):
    (path / name).write_text(json.dumps(data), encoding="utf-8")


def test_export_import_roundtrip(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    write(src, "stats.json", {"1": {"players": {"10": {"wins": 3}}}})
    write(src, "memory.json", {"1": {}})
    write(src, "vpn.json", {"selected": {"host": "secret"}})
    write(src, "llm_usage.json", {"day": "x"})
    payload, included = datasync.build_archive(src, [1])
    assert included == ["stats.json", "memory.json"]
    names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
    assert "vpn.json" not in names and "llm_usage.json" not in names

    write(dst, "stats.json", {"old": True})
    write(dst, "oleg.json", {"keep": True})
    files, meta = datasync.read_archive(payload)
    assert meta["guilds"] == ["1"]
    backup = datasync.apply_archive(dst, files)
    assert json.loads((dst / "stats.json").read_text())["1"]["players"]["10"]["wins"] == 3
    assert json.loads((dst / "oleg.json").read_text()) == {"keep": True}  # не было в архиве — не тронут
    assert json.loads((backup / "stats.json").read_text()) == {"old": True}
    assert "<t:" in datasync.describe_meta(meta)


def zip_of(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_bad_archives_are_rejected():
    meta = json.dumps({"format": datasync.FORMAT_VERSION})
    with pytest.raises(datasync.ImportError_):
        datasync.read_archive(b"not a zip")
    with pytest.raises(datasync.ImportError_):
        datasync.read_archive(zip_of({"stats.json": "{}"}))  # нет описания
    with pytest.raises(datasync.ImportError_):
        datasync.read_archive(zip_of({datasync.META_FILE: meta, "stats.json": "{broken"}))
    with pytest.raises(datasync.ImportError_):
        datasync.read_archive(zip_of({datasync.META_FILE: meta}))  # нет данных
    # чужие файлы (например, ../evil или vpn.json) просто игнорируются
    files, _ = datasync.read_archive(zip_of({
        datasync.META_FILE: meta, "stats.json": "{}", "../evil.json": "{}", "vpn.json": "{}",
    }))
    assert set(files) == {"stats.json"}
