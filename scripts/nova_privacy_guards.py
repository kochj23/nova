#!/usr/bin/env python3
"""nova_privacy_guards.py — P3: camera and face data are for safety and presence, nothing else.

Producers (nova_face_recognition, nova_face_integration) tag every face output with
tag_private(). Content generators (journal, reach, essays, digests to the herd) pass recalled
memories through filter_for_content() and draft text through scrub_face_mentions(). A recognised
face never becomes material for persuasion, courting, engagement, or a message to a third party.

  tag_private(metadata, kind)        -> metadata + {"privacy":"private","purpose":[safety,presence],...}
  is_face_output(memory)             -> True for a face/camera output (by source, tag or text shape)
  filter_for_content(memories)       -> memories minus face/camera outputs
  scrub_face_mentions(text, names)   -> text with "<Name> was seen/detected at <camera>" sentences removed
  camera_use_ok(purpose, recipient)  -> (ok, reason): purpose must be safety/presence, recipient must
                                        be Jordan or an internal store, never a third party

Pure functions, no I/O. Owned by the safety lane. Written by Jordan Koch.
"""
from __future__ import annotations

import re

ALLOWED_PURPOSES = frozenset({"safety", "presence"})
INTERNAL_RECIPIENTS = frozenset({"jordan", "self", "internal", "presence_engine", "security_organ",
                                 "nova_ops", "telemetry", "memory", "slack:jordan", "nova-chat", "nova-warning"})
FACE_SOURCES = frozenset({"face_recognition", "face_presence", "face_integration", "camera_presence",
                          "face_gate", "frigate_face", "camera_face"})
FACE_TYPES_RX = re.compile(r"^(face|camera)_", re.I)
_FACE_TEXT_RX = re.compile(
    r"\b(face|faces)\b.{0,40}\b(detected|recogni[sz]ed|matched|spotted|identified)\b|"
    r"\b[A-Z][a-z]+\b (was |is )?(detected|spotted|seen|recogni[sz]ed) (at|on|by) (the )?\w*[ _-]?(cam|camera|doorbell|gate|porch|driveway)\w*|"
    r"\b(unknown|known) face\b|\(\d{1,3}% match\)", re.I)


def tag_private(metadata: dict | None, kind: str = "face") -> dict:
    """Return a copy of metadata tagged as private safety/presence data."""
    md = dict(metadata or {})
    md.update({"privacy": "private", "purpose": sorted(ALLOWED_PURPOSES), "data_class": kind,
               "no_content_generation": True, "no_third_party": True})
    return md


def is_face_output(memory: dict) -> bool:
    if not isinstance(memory, dict):
        return False
    src = str(memory.get("source") or "").lower().removeprefix("quarantine:")
    if src in FACE_SOURCES:
        return True
    md = memory.get("metadata") or {}
    if isinstance(md, dict):
        if md.get("privacy") == "private" or md.get("no_content_generation"):
            return True
        if FACE_TYPES_RX.search(str(md.get("type") or "")) or md.get("data_class") in ("face", "camera"):
            return True
    return bool(_FACE_TEXT_RX.search(str(memory.get("text") or memory.get("content") or "")))


def filter_for_content(memories) -> list:
    """Drop face/camera outputs before anything is generated from these memories."""
    return [m for m in (memories or []) if not is_face_output(m)]


def scrub_face_mentions(text: str, names=()) -> str:
    """Remove sentences that report a face sighting (generic shape, or a named person + camera)."""
    if not text:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    name_rx = None
    if names:
        name_rx = re.compile(r"\b(" + "|".join(re.escape(n) for n in names if n) + r")\b.{0,60}\b(camera|cam|"
                             r"doorbell|face|spotted|detected|seen on|seen at|arrived home)\b", re.I)
    keep = [s for s in sentences
            if not _FACE_TEXT_RX.search(s) and not (name_rx and name_rx.search(s))]
    return " ".join(keep)


def camera_use_ok(purpose: str, recipient: str = "internal") -> tuple:
    p = (purpose or "").strip().lower()
    r = (recipient or "").strip().lower()
    if p not in ALLOWED_PURPOSES:
        return False, f"camera/face data may serve safety or presence only, not '{purpose}'"
    if r not in INTERNAL_RECIPIENTS:
        return False, f"camera/face data never goes to a third party ('{recipient}')"
    return True, "safety/presence use, internal recipient"
