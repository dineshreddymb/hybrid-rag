import io
import zipfile
from unittest.mock import MagicMock, patch

from backend_app.ingestion import remote_sync


def _create_sample_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("compliance/audit.md", "# Compliance Audit\nContent for audit.")
        z.writestr("compliance/sub/gdpr.txt", "GDPR compliance details.")
        z.writestr("engineering/api.md", "# API Design\nREST and gRPC specs.")
        z.writestr("engineering/backend.md", "# Backend Architecture\nFastAPI details.")
        z.writestr("hr/handbook.md", "# HR Handbook\nEmployee benefits.")
        z.writestr("readme.txt", "Root level readme file.")
        z.writestr("ignored/image.png", b"\x89PNG\r\n\x1a\n...")
    return buf.getvalue()


def test_group_zip_by_folders():
    zip_bytes = _create_sample_zip_bytes()
    groups = remote_sync.group_zip_by_folders(zip_bytes)

    assert "compliance" in groups
    assert "engineering" in groups
    assert "hr" in groups
    assert "root" in groups
    assert "ignored" not in groups  # non-text files are ignored

    compliance_files = [m.filename for m in groups["compliance"]]
    assert "compliance/audit.md" in compliance_files
    assert "compliance/sub/gdpr.txt" in compliance_files

    assert len(groups["engineering"]) == 2
    assert len(groups["hr"]) == 1
    assert len(groups["root"]) == 1


def test_crawl_folder_worker():
    zip_bytes = _create_sample_zip_bytes()
    groups = remote_sync.group_zip_by_folders(zip_bytes)

    items, stats = remote_sync._crawl_folder_worker("engineering", groups["engineering"], zip_bytes, force=False)

    assert len(items) == 2
    assert stats["folder"] == "engineering"
    assert stats["files_count"] == 2
    assert stats["total_bytes"] > 0
    assert stats["duration_ms"] >= 0

    sources = [i["source"] for i in items]
    assert "engineering/api.md" in sources
    assert "engineering/backend.md" in sources
    assert all(i["origin"] == "cloud_sync" for i in items)


def test_sync_remote_zip_orchestration_and_idempotency():
    zip_bytes = _create_sample_zip_bytes()

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {
        "ETag": '"etag-12345"',
        "Last-Modified": "Mon, 23 Nov 2026 09:00:00 GMT",
        "Content-Length": str(len(zip_bytes)),
    }
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = zip_bytes

    with patch("urllib.request.urlopen", return_value=mock_resp), \
         patch("backend_app.ingestion.pipeline.ingest_items") as mock_ingest:
        mock_ingest.return_value = {
            "job_id": "job_test_123",
            "results": [
                {"source": "compliance/audit.md", "status": "ingested", "chunks_new": 2},
                {"source": "engineering/api.md", "status": "ingested", "chunks_new": 3},
            ],
        }

        # First sync: should download and ingest
        res = remote_sync.sync_remote_zip(url="https://mock.supabase.co/docs.zip", force=True)
        assert res["status"] == "synced"
        assert res["etag"] == "etag-12345"
        assert res["total_files"] == 6  # 2 compliance + 2 engineering + 1 hr + 1 root
        assert res["total_folders"] == 4
        assert mock_ingest.called

        # Second sync without force: same ETag, should skip download and ingest
        mock_ingest.reset_mock()
        res_skip = remote_sync.sync_remote_zip(url="https://mock.supabase.co/docs.zip", force=False)
        assert res_skip["status"] == "unchanged"
        assert not mock_ingest.called


def test_sync_status_telemetry():
    status = remote_sync.get_sync_status()
    assert "enabled" in status
    assert "url" in status
    assert "last_status" in status
    assert "cached_folders" in status


def test_sync_direct_markdown_file():
    md_content = b"# Direct Cloud Document\nThis is a markdown file directly in cloud storage."
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {
        "ETag": '"etag-direct-md"',
        "Last-Modified": "Mon, 23 Nov 2026 09:30:00 GMT",
        "Content-Length": str(len(md_content)),
    }
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = md_content

    with patch("urllib.request.urlopen", return_value=mock_resp), \
         patch("backend_app.ingestion.pipeline.ingest_items") as mock_ingest:
        mock_ingest.return_value = {
            "job_id": "job_direct_md",
            "results": [{"source": "compliance/policy.md", "status": "ingested", "chunks_new": 1}],
        }

        url = "https://mock.supabase.co/storage/v1/object/public/docs/compliance/policy.md"
        res = remote_sync.sync_remote_zip(url=url, force=True)
        assert res["status"] == "synced"
        assert res["total_files"] == 1
        assert res["total_folders"] == 1
        assert res["folder_metrics"][0]["folder"] == "compliance"
        assert mock_ingest.called

        items_passed = mock_ingest.call_args[0][0]
        assert len(items_passed) == 1
        assert items_passed[0]["source"] == "compliance/policy.md"
        assert "Direct Cloud Document" in items_passed[0]["content"]


def test_sync_supabase_bucket_discovery():
    sample_zip = _create_sample_zip_bytes()
    sample_txt = b"New extra document text."

    mock_objects = [
        {"name": "docs.zip", "metadata": {"eTag": "etag-zip-1"}},
        {"name": "docs2.txt", "metadata": {"eTag": "etag-txt-2"}},
    ]

    def mock_download(url, timeout=45.0):
        if url.endswith("docs.zip"):
            return sample_zip
        return sample_txt

    with patch("backend_app.ingestion.remote_sync.list_supabase_bucket_objects", return_value=mock_objects), \
         patch("backend_app.ingestion.remote_sync.download_remote_zip", side_effect=mock_download), \
         patch("backend_app.ingestion.pipeline.ingest_items") as mock_ingest:
        mock_ingest.return_value = {
            "job_id": "job_bucket_sync",
            "results": [{"source": "docs2.txt", "status": "ingested", "chunks_new": 1}],
        }

        res = remote_sync.sync_supabase_bucket(
            supabase_url="https://mock.supabase.co",
            bucket="docs",
            anon_key="fake-anon-key",
            force=True,
        )

        assert res["status"] == "synced"
        assert res["bucket"] == "docs"
        assert "docs.zip" in res["synced_objects"]
        assert "docs2.txt" in res["synced_objects"]
        assert mock_ingest.called

        items = mock_ingest.call_args[0][0]
        sources = [i["source"] for i in items]
        assert "docs2.txt" in sources
        assert "compliance/audit.md" in sources  # from zip
        assert "engineering/api.md" in sources   # from zip

