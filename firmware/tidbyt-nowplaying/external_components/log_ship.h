#pragma once
// v0.4.13: ship device logs over WiFi so nobody ever opens a serial page.
//
// v0.4.14: capture now goes through the logger's on_message trigger in the
// main YAML (it requests the USE_LOG_LISTENERS slot for us — the raw
// add_log_callback API compiles to a no-op stub without it). This header
// only holds the batch buffer the trigger appends to and the ship interval
// POSTs. Included into main.cpp (single translation unit), like art_buf.h.
//
// Memory care: the batch is capped at 8KB in INTERNAL heap (a 5s DEBUG
// batch is ~1-4KB in practice). On a failed POST the batch is dropped —
// this is a live view, not a forensic archive. The in-sink guard prevents
// recursion: our own "shipping" log lines must not append to the batch
// they are shipping.

#include <string>

static std::string ta_log_batch;
static bool ta_log_in_sink = false;

static void ta_log_append(uint8_t level, const char *tag, const char *msg) {
  if (ta_log_in_sink)
    return;  // our own shipping logs do not recurse into the batch
  if (ta_log_batch.size() > 8192)
    return;  // batch full — newest lines win next tick
  static const char LEVEL_CHARS[] = {'N', 'E', 'W', 'I', 'D', 'V', 'V'};
  char lvl = (level < sizeof(LEVEL_CHARS)) ? LEVEL_CHARS[level] : '?';
  std::string line = esphome::str_sprintf("[%c][%s] %s", lvl, tag, msg);
  while (!line.empty() && (line.back() == '\n' || line.back() == '\r'))
    line.pop_back();
  ta_log_batch += line;
  ta_log_batch += '\n';
}

// Copy-and-clear for the POST body. The copy keeps any lines the logger
// appends while the HTTP action is in flight from being lost.
static std::string ta_log_take() {
  ta_log_in_sink = true;
  std::string out;
  out.swap(ta_log_batch);
  ta_log_in_sink = false;
  return out;
}

static bool ta_log_have() { return !ta_log_batch.empty(); }

