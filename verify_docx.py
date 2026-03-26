#!/usr/bin/env python
"""Verify generated Word document content."""

from docx import Document
import sys

if len(sys.argv) < 2:
    print("Usage: python verify_docx.py <docx_file>")
    sys.exit(1)

doc_path = sys.argv[1]
doc = Document(doc_path)

print("=" * 80)
print(f"Document: {doc_path}")
print("=" * 80)
print()

for i, para in enumerate(doc.paragraphs, 1):
    text = para.text.strip()
    if text:
        print(f"[{i}] {text}")

print()
print("=" * 80)
