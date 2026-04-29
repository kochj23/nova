#!/usr/bin/env python3
"""
ingest_manuals.py — Ingest all documents from /Volumes/nas/GoogleDriveBackups/Manuals/
into Nova's vector memory. Handles PDFs, DOCX, XLSX, and text files.

Chunks large documents into ~500-word segments for optimal vector search.
"""
import json, os, sys, time, subprocess, urllib.request
from pathlib import Path
from datetime import datetime

MEMORY_URL = "http://127.0.0.1:18790/remember"
MANUALS_DIR = Path("/Volumes/nas/GoogleDriveBackups/Manuals")
LOG_FILE = Path.home() / ".openclaw/logs/ingest-manuals.log"
CHUNK_SIZE = 500  # words per chunk

count = 0
failed = 0
skipped = 0

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[ingest_manuals {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def remember(text, source="document", metadata=None):
    global count, failed
    if not text.strip() or len(text.strip()) < 20:
        return False
    payload = json.dumps({
        "text": text[:2000],  # Max 2000 chars per memory
        "source": source,
        "metadata": metadata or {}
    }).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15):
            count += 1
            return True
    except Exception as e:
        failed += 1
        if failed % 10 == 0:
            log(f"  Failed ({failed} total): {e}")
        time.sleep(1)
        return False

def extract_pdf(filepath):
    """Extract text from PDF using pdftotext or python."""
    try:
        # Try pdftotext first (fast, installed via poppler)
        result = subprocess.run(
            ["pdftotext", "-layout", str(filepath), "-"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except FileNotFoundError:
        pass
    
    # Fallback: try PyPDF2 or pdfminer
    try:
        import PyPDF2
        with open(filepath, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            text = ""
            for page in reader.pages[:100]:  # Max 100 pages
                text += page.extract_text() or ""
            return text
    except ImportError:
        pass
    except Exception as e:
        log(f"  PDF extraction failed for {filepath.name}: {e}")
    
    return ""

def extract_docx(filepath):
    """Extract text from DOCX."""
    try:
        import docx
        doc = docx.Document(str(filepath))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        # Fallback: use textutil (macOS built-in)
        try:
            result = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(filepath)],
                capture_output=True, text=True, timeout=30
            )
            return result.stdout
        except:
            return ""
    except Exception as e:
        log(f"  DOCX extraction failed for {filepath.name}: {e}")
        return ""

def extract_xlsx(filepath):
    """Extract text from XLSX."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
        text_parts = []
        for sheet in wb.worksheets[:5]:  # Max 5 sheets
            for row in sheet.iter_rows(max_row=200, values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    text_parts.append(" | ".join(cells))
        return "\n".join(text_parts)
    except ImportError:
        return ""
    except Exception as e:
        log(f"  XLSX extraction failed for {filepath.name}: {e}")
        return ""

def chunk_text(text, chunk_size=CHUNK_SIZE):
    """Split text into chunks of approximately chunk_size words."""
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size])
        if len(chunk.strip()) > 50:  # Skip tiny fragments
            chunks.append(chunk)
    return chunks

def process_file(filepath):
    """Extract and ingest a single file."""
    global skipped
    ext = filepath.suffix.lower()
    
    # Skip non-document files
    if ext in ['.ds_store', '.exe', '.zip', '.rar', '.jpg', '.png', '.gif']:
        skipped += 1
        return 0
    
    # Extract text based on file type
    if ext == '.pdf':
        text = extract_pdf(filepath)
    elif ext == '.docx':
        text = extract_docx(filepath)
    elif ext in ['.xlsx', '.xls']:
        text = extract_xlsx(filepath)
    elif ext in ['.txt', '.md', '.csv']:
        try:
            text = filepath.read_text(encoding="utf-8", errors="ignore")
        except:
            text = ""
    else:
        skipped += 1
        return 0
    
    if not text or len(text.strip()) < 50:
        log(f"  Skipped (no extractable text): {filepath.name}")
        skipped += 1
        return 0
    
    # Determine source category
    rel_path = str(filepath.relative_to(MANUALS_DIR))
    if "corvette" in rel_path.lower():
        source = "corvette_workshop_manual"
        meta_type = "vehicle_manual"
    elif "ssl" in rel_path.lower():
        source = "ssl_management"
        meta_type = "work_document"
    elif "git" in rel_path.lower():
        source = "git_training"
        meta_type = "training_document"
    else:
        source = "document"
        meta_type = "manual"
    
    # Chunk and ingest
    chunks = chunk_text(text)
    ingested = 0
    for i, chunk in enumerate(chunks):
        metadata = {
            "type": meta_type,
            "file": filepath.name,
            "path": rel_path,
            "chunk": i + 1,
            "total_chunks": len(chunks)
        }
        if remember(chunk, source=source, metadata=metadata):
            ingested += 1
        
        # Rate limit slightly
        if ingested % 50 == 0 and ingested > 0:
            time.sleep(0.5)
    
    return ingested

def main():
    log("=" * 60)
    log("Starting manuals ingestion")
    log(f"Source: {MANUALS_DIR}")
    
    # Find all files
    all_files = [f for f in MANUALS_DIR.rglob("*") if f.is_file() and not f.name.startswith(".")]
    log(f"Found {len(all_files)} files to process")
    
    total_ingested = 0
    for i, filepath in enumerate(all_files):
        log(f"[{i+1}/{len(all_files)}] Processing: {filepath.name} ({filepath.suffix})")
        ingested = process_file(filepath)
        total_ingested += ingested
        if ingested > 0:
            log(f"  → Ingested {ingested} chunks")
    
    log("=" * 60)
    log(f"INGESTION COMPLETE")
    log(f"  Files processed: {len(all_files)}")
    log(f"  Chunks ingested: {count}")
    log(f"  Failed: {failed}")
    log(f"  Skipped: {skipped}")
    log("=" * 60)
    
    # Post to Slack
    sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
    import nova_config
    nova_config.post_both(
        f":books: *Manual Ingestion Complete*\n"
        f"  Source: `/Volumes/nas/GoogleDriveBackups/Manuals/`\n"
        f"  Files: {len(all_files)}\n"
        f"  Chunks ingested: {count:,}\n"
        f"  Failed: {failed} | Skipped: {skipped}",
        slack_channel=nova_config.SLACK_NOTIFY
    )

if __name__ == "__main__":
    main()
