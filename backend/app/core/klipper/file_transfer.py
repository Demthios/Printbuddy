"""
backend/app/printers/klipper/file_transfer.py
==============================================
Upload gcode files to Moonraker using its HTTP file upload API.

Moonraker does NOT use FTP (unlike Bambu).  It provides a standard
multipart/form-data HTTP endpoint:

    POST http://<host>:7125/server/files/upload

This is much simpler than Bambu's FTPS flow — we just POST the file
and Moonraker puts it in ~/printer_data/gcodes/ (or a subfolder).

After uploading we can optionally tell Moonraker to start printing via:
    POST http://<host>:7125/printer/print/start
    Body: {"filename": "yourfile.gcode"}

We use aiohttp for async HTTP because FastAPI's httpx would also work
but aiohttp is already a common dependency in printer projects.
"""

import logging
from io import BytesIO
from typing import Optional

import aiohttp  # pip install aiohttp

logger = logging.getLogger(__name__)


class KlipperFileTransfer:
    """
    Handles gcode file uploads to a Moonraker instance.

    Usage:
        transfer = KlipperFileTransfer(host="192.168.1.50", port=7125)
        ok = await transfer.upload("benchy.gcode", file_bytes)
        if ok:
            await transfer.start_print("benchy.gcode")
    """

    def __init__(
        self,
        host: str,
        port: int = 7125,
        api_key: Optional[str] = None,
        upload_path: str = "",  # Subfolder inside gcodes dir, e.g. "printbuddy"
    ):
        self.host = host
        self.port = port
        self.api_key = api_key
        # Files are stored under gcodes/<upload_path>/<filename>
        # Leave empty to put files directly in the gcodes root
        self.upload_path = upload_path.strip("/")

        self._base_url = f"http://{host}:{port}"

    def _headers(self) -> dict:
        """Build HTTP headers, including auth if we have an API key."""
        headers = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    async def upload(
        self,
        filename: str,
        file_bytes: bytes,
        print_after: bool = False,
    ) -> bool:
        """
        Upload a gcode file to Moonraker.

        Args:
            filename:    Target filename on the printer (e.g. "part.gcode")
            file_bytes:  Raw gcode content
            print_after: If True, tell Moonraker to start printing immediately
                         after the upload completes.

        Returns:
            True on success, False on failure.
        """
        url = f"{self._base_url}/server/files/upload"

        # Build the full path Moonraker should store the file at.
        # If upload_path is set, it becomes a subfolder under gcodes/.
        if self.upload_path:
            moonraker_path = f"{self.upload_path}/{filename}"
        else:
            moonraker_path = filename

        logger.info(f"Uploading {filename} ({len(file_bytes)} bytes) to {self.host}")

        try:
            # aiohttp.FormData handles multipart encoding for us —
            # equivalent to filling out an HTML file upload form.
            form = aiohttp.FormData()
            form.add_field("root", "gcodes")  # Upload into the gcodes virtual drive

            if self.upload_path:
                form.add_field("path", self.upload_path)  # Subfolder

            form.add_field(
                "file",
                BytesIO(file_bytes),
                filename=filename,
                content_type="application/octet-stream",
            )

            if print_after:
                form.add_field("print", "true")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=form,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=300),  # 5 min for large files
                ) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        logger.info(
                            f"Upload succeeded: {result.get('item', {}).get('path', filename)}"
                        )
                        return True
                    else:
                        body = await resp.text()
                        logger.error(
                            f"Upload failed: HTTP {resp.status} — {body[:500]}"
                        )
                        return False

        except aiohttp.ClientError as exc:
            logger.error(f"Network error during upload to {self.host}: {exc}")
            return False
        except Exception as exc:
            logger.error(f"Unexpected error during upload: {exc}")
            return False

    async def start_print(self, filename: str) -> bool:
        """
        Tell Moonraker to start printing a file that is already uploaded.

        Args:
            filename: Path relative to gcodes root, e.g. "part.gcode"
                      or "printbuddy/part.gcode" if using a subfolder.

        Returns:
            True on success.
        """
        if self.upload_path:
            full_path = f"{self.upload_path}/{filename}"
        else:
            full_path = filename

        url = f"{self._base_url}/printer/print/start"
        logger.info(f"Starting print: {full_path} on {self.host}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"filename": full_path},
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        logger.info(f"Print started: {full_path}")
                        return True
                    else:
                        body = await resp.text()
                        logger.error(
                            f"Failed to start print: HTTP {resp.status} — {body[:300]}"
                        )
                        return False

        except aiohttp.ClientError as exc:
            logger.error(f"Network error starting print on {self.host}: {exc}")
            return False

    async def list_files(self, path: str = "") -> list[dict]:
        """
        List gcode files on the printer.

        Returns:
            List of dicts with 'filename', 'size', 'modified' fields.
            Returns [] on error.
        """
        url = f"{self._base_url}/server/files/list"
        params = {"root": "gcodes"}
        if path:
            params["path"] = path

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", [])
                    return []

        except Exception as exc:
            logger.error(f"Failed to list files on {self.host}: {exc}")
            return []

    async def delete_file(self, filename: str) -> bool:
        """
        Delete a gcode file from the printer.

        Args:
            filename: Relative path from gcodes root.
        """
        if self.upload_path:
            full_path = f"gcodes/{self.upload_path}/{filename}"
        else:
            full_path = f"gcodes/{filename}"

        url = f"{self._base_url}/server/files/{full_path}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    url,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    return resp.status == 200

        except Exception as exc:
            logger.error(f"Failed to delete {filename} on {self.host}: {exc}")
            return False
