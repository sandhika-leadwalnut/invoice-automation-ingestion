"""
Turn Word attachments into PDFs so the rest of the pipeline never has to know
they were not PDFs to begin with.

The design choice here is deliberate: convert at the edge, keep one extraction
path. Unstract, the duplicate checks, Drive and the review UI all continue to
see a PDF, so nothing downstream changes and there is only ever one set of
parsing bugs to fix.

Conversion runs through LibreOffice headless, which handles both .doc and
.docx and keeps every invoice inside our own infrastructure.

Two operational details matter more than they look:

  - soffice shares a profile directory between instances and corrupts it when
    two run at once. With a 60-second poll and several attachments per email
    that would happen quickly, so conversions are serialised behind a lock.
  - soffice blocks forever on a password-protected or malformed file. Without
    a timeout a single bad attachment stops the poller permanently, and the
    only symptom is invoices quietly ceasing to arrive. Hence the timeout and
    the kill.

Output goes to /tmp, which is a tmpfs on the server - the conversion never
touches the disk.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading

logger = logging.getLogger(__name__)

# Extensions this module can turn into a PDF. A PDF passes through untouched.
WORD_EXTENSIONS = (".doc", ".docx", ".rtf", ".odt")
SUPPORTED_EXTENSIONS = (".pdf",) + WORD_EXTENSIONS

# Long enough for a large document on a loaded box, short enough that a stuck
# conversion does not hold the poller past its next run.
CONVERT_TIMEOUT_SECONDS = int(os.getenv("CONVERT_TIMEOUT_SECONDS", "90"))

# soffice cannot safely run twice at once against the same profile.
_convert_lock = threading.Lock()


class ConversionError(Exception):
    """Raised when a document could not be turned into a PDF."""


def is_supported(filename: str) -> bool:
    return bool(filename) and filename.lower().endswith(SUPPORTED_EXTENSIONS)


def needs_conversion(filename: str) -> bool:
    return bool(filename) and filename.lower().endswith(WORD_EXTENSIONS)


def _soffice_binary() -> str:
    for candidate in ("soffice", "libreoffice"):
        found = shutil.which(candidate)
        if found:
            return found
    raise ConversionError(
        "LibreOffice is not installed in this container. Word attachments "
        "cannot be converted until it is - see the Dockerfile."
    )


def convert_to_pdf(filename: str, data: bytes) -> bytes:
    """
    Convert a Word document to PDF and return the PDF bytes.

    Raises ConversionError rather than returning something half-usable: an
    invoice that failed to convert must be visible, not silently skipped.
    """
    if not needs_conversion(filename):
        raise ConversionError(f"{filename!r} is not a document this can convert")

    binary = _soffice_binary()
    suffix = os.path.splitext(filename)[1].lower()

    # dir=/tmp keeps the work in tmpfs. Everything here is removed on exit,
    # including after a failure, so a stuck conversion cannot accumulate files.
    with tempfile.TemporaryDirectory(prefix="invoice-convert-", dir="/tmp") as workdir:
        source = os.path.join(workdir, f"source{suffix}")
        with open(source, "wb") as handle:
            handle.write(data)

        # A profile per conversion removes the shared-state problem entirely;
        # the lock below then only guards against the cost of several heavy
        # LibreOffice processes starting at once.
        profile = os.path.join(workdir, "profile")
        command = [
            binary,
            "--headless", "--norestore", "--invisible", "--nolockcheck",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf",
            "--outdir", workdir,
            source,
        ]

        with _convert_lock:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=CONVERT_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ConversionError(
                    f"LibreOffice did not finish converting {filename!r} within "
                    f"{CONVERT_TIMEOUT_SECONDS}s. The file may be password "
                    f"protected or corrupt."
                )

        produced = os.path.join(workdir, "source.pdf")
        if not os.path.exists(produced):
            stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise ConversionError(
                f"LibreOffice produced no PDF for {filename!r} "
                f"(exit {result.returncode}). {stderr[:400]}"
            )

        with open(produced, "rb") as handle:
            pdf = handle.read()

    if not pdf.startswith(b"%PDF"):
        raise ConversionError(f"Output for {filename!r} is not a valid PDF")

    logger.info(
        "Converted %s to PDF (%d KB in, %d KB out)",
        filename, len(data) // 1024, len(pdf) // 1024,
    )
    return pdf


def pdf_filename_for(filename: str) -> str:
    """invoice.docx -> invoice.pdf. Keeps the stem so Drive stays readable."""
    return os.path.splitext(filename)[0] + ".pdf"
