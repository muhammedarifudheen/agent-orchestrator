"""Spec ingestors: turn an OpenAPI document, pasted docs, or recorded HTTP traffic into a draft system
spec (the format in specs/schema.json). Ingestors only DRAFT. They never generate servers and never
decide that anything is safe: operations they are unsure about, and every write, come out with
``needs_review: true`` and the pipeline refuses to generate from them until a person clears the flag.
"""
