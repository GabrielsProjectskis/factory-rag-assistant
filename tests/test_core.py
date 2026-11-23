"""Core behaviour, run against the model-free demo provider:  pytest"""
import asyncio
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
for _role in ("CHAT", "VISION", "EMBEDDING"):
    os.environ[f"{_role}_PROVIDER"] = "demo"
    os.environ[f"{_role}_MODEL"] = "demo"
os.environ["PRIVATE_MODE"] = "true"

import pytest  # noqa: E402

from app import config, db, ics, ingest, providers, retrieval  # noqa: E402

db.init()
ics.import_csv(b"ICS code;Naam;Omschrijving\n20-PMP-001;Cooling pump A;Circulates water\nXY/77;Odd code;Free-form code\n")


def _add(name: str, text: str) -> str:
    doc_id = ingest.create_document(name, text.encode())
    asyncio.run(ingest.process_document(doc_id))
    return doc_id


def test_chunking_keeps_everything_and_respects_size():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(12))
    chunks = ingest.chunk_text(text, max_chars=600, overlap=80)
    assert len(chunks) > 3
    assert all(len(c) <= 600 + 80 + 2 for c in chunks)
    assert all(f"Paragraph {i} " in " ".join(chunks) for i in range(12))


def test_registry_csv_accepts_semicolons_and_dutch_headers():
    assert ics.get("20-pmp-001")["name"] == "Cooling pump A"


def test_codes_found_by_registry_and_by_pattern():
    found = ics.find_codes("Check xy/77 and 20-PMP-001, then 30-CMP-009. Not M12 bolts.")
    assert found == ["XY/77", "20-PMP-001", "30-CMP-009"]  # registry spelling; pattern catches unregistered


def test_retrieval_prefers_the_part_that_was_asked_for():
    _add("pump_a.md", "Pump 20-PMP-001: grease the motor bearings every 3 months.")
    _add("pump_b.md", "Pump 20-PMP-002: grease the motor bearings every 6 months.")
    _add("canteen.txt", "The canteen serves lunch between 12:00 and 13:00.")
    results, warning = asyncio.run(retrieval.search("How often do I grease 20-PMP-001?", config.load()))
    assert warning is None
    assert results[0]["filename"] == "pump_a.md"
    assert "20-PMP-001" in results[0]["codes"]


def test_delete_removes_document_from_search():
    doc_id = _add("temp.md", "Zebra crossing procedure for forklifts.")
    assert ingest.delete_document(doc_id)
    results, _ = asyncio.run(retrieval.search("zebra forklifts", config.load()))
    assert all(r["doc_id"] != doc_id for r in results)


def test_private_mode_blocks_cloud_providers():
    settings = config.load()
    with pytest.raises(providers.ProviderError):
        providers.get("google", settings)
    assert config.is_local_url("http://ollama:11434") and config.is_local_url("http://192.168.1.20:11434")
    assert not config.is_local_url("https://api.example.com/v1")


def test_title_code_applies_to_every_passage_of_a_manual():
    body = "# Manual for pump 20-PMP-001\n\n" + "\n\n".join(
        f"Section {i}. " + "General pump text without any code. " * 25 for i in range(4))
    doc_id = _add("manual.md", body)
    rows = db.query("SELECT c.id, (SELECT COUNT(*) FROM chunk_codes cc WHERE cc.chunk_id = c.id "
                    "AND cc.code = '20-PMP-001') AS tagged FROM chunks c WHERE doc_id=?", (doc_id,))
    assert len(rows) > 1 and all(r["tagged"] for r in rows)
