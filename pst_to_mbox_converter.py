#!/usr/bin/env python3
"""
PST to MBOX converter with progress tracking.

Converts all PST files in a folder to individual MBOX files.
Requires: readpst (from libpst) to be installed on Windows.
Install via: choco install libpst (or download from https://www.five-ten-sg.com/libpst/)
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path
from datetime import datetime


class PST2MBOXConverter:
    def __init__(self, input_dir: str, output_dir: str = None):
        self.input_dir = Path(input_dir).resolve()
        self.output_dir = Path(output_dir or input_dir).resolve()
        self.pst_files = []
        self.results = {"success": [], "failed": []}

    def discover_pst_files(self):
        """Find all PST files in the input directory."""
        self.pst_files = sorted(self.input_dir.glob("*.pst"))
        if not self.pst_files:
            self.log("⚠️  No PST files found in: " + str(self.input_dir))
            return False
        self.log(f"📁 Found {len(self.pst_files)} PST file(s)")
        return True

    def log(self, message: str, level: str = "INFO"):
        """Log with timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = {
            "INFO": "ℹ️ ",
            "ERROR": "❌",
            "SUCCESS": "✅",
            "PROGRESS": "⏳",
        }.get(level, "")
        print(f"[{timestamp}] {prefix} {message}")

    def check_readpst(self) -> bool:
        """Verify readpst is installed."""
        try:
            subprocess.run(["readpst", "-h"], capture_output=True, check=False)
            return True
        except FileNotFoundError:
            self.log(
                "readpst not found. Install with: choco install libpst",
                "ERROR",
            )
            return False

    def convert_pst_to_mbox(self, pst_file: Path) -> bool:
        """Convert a single PST file to MBOX format."""
        try:
            self.log(f"Converting: {pst_file.name}", "PROGRESS")

            # readpst -o outputs to MBOX format in the specified directory
            # -r flag outputs in MBOX format (RFC 4155)
            cmd = [
                "readpst",
                "-o",
                str(self.output_dir),
                "-r",
                str(pst_file),
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min per file
            )

            if result.returncode != 0:
                self.log(
                    f"Failed to convert {pst_file.name}: {result.stderr}",
                    "ERROR",
                )
                return False

            # readpst creates files with .mbox extension, rename if needed
            mbox_file = self.output_dir / f"{pst_file.stem}.mbox"
            if mbox_file.exists():
                file_size_mb = mbox_file.stat().st_size / (1024 * 1024)
                self.log(
                    f"✅ {pst_file.name} → {mbox_file.name} ({file_size_mb:.1f} MB)",
                    "SUCCESS",
                )
                self.results["success"].append(pst_file.name)
                return True
            else:
                self.log(f"Output file not created for {pst_file.name}", "ERROR")
                return False

        except subprocess.TimeoutExpired:
            self.log(f"Timeout converting {pst_file.name} (>5 min)", "ERROR")
            return False
        except Exception as e:
            self.log(f"Error converting {pst_file.name}: {str(e)}", "ERROR")
            return False

    def convert_all(self):
        """Convert all PST files."""
        if not self.discover_pst_files():
            return False

        if not self.check_readpst():
            return False

        self.log(f"Starting conversion of {len(self.pst_files)} files...", "INFO")
        print()

        for i, pst_file in enumerate(self.pst_files, 1):
            status = f"[{i}/{len(self.pst_files)}]"
            print(f"\n{status} " + "=" * 50)
            self.convert_pst_to_mbox(pst_file)

        print("\n" + "=" * 50)
        self.print_summary()
        return len(self.results["failed"]) == 0

    def print_summary(self):
        """Print conversion summary."""
        total = len(self.pst_files)
        success = len(self.results["success"])
        failed = len(self.results["failed"])

        self.log(f"Conversion complete: {success}/{total} succeeded", "INFO")

        if self.results["success"]:
            print("\n✅ Successfully converted:")
            for fname in self.results["success"]:
                print(f"   • {fname}")

        if self.results["failed"]:
            print("\n❌ Failed conversions:")
            for fname in self.results["failed"]:
                print(f"   • {fname}")

        print(f"\n📂 Output directory: {self.output_dir}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python pst_to_mbox_converter.py <input_folder> [output_folder]")
        print("\nExample:")
        print("  python pst_to_mbox_converter.py C:\\PST_Files")
        print("  python pst_to_mbox_converter.py C:\\PST_Files C:\\MBOX_Output")
        sys.exit(1)

    input_dir = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else input_dir

    if not Path(input_dir).exists():
        print(f"Error: Input directory not found: {input_dir}")
        sys.exit(1)

    converter = PST2MBOXConverter(input_dir, output_dir)
    success = converter.convert_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
