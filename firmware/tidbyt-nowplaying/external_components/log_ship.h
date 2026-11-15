#pragma once
// v0.4.13: ship device logs over WiFi so nobody ever opens a serial page.
//
// Registers one callback on ESPHome's logger; every line is appended to a
// small batch string. The main YAML's 5s interval POSTs the batch to the
// tonearm server and clears it. Included into main.cpp (single translation
// unit), same pattern as art_buf.h.
//
// Memory care: the batch is capped at 8KB in INTERNAL heap (a 5s DEBUG
// batch is ~1-4KB in practice). No ring buffer, no PSRAM dependency: on a
// failed POST the batch is simply dropped — this is a live view, not a
// forensic archive. The capture guard prevents recursion: our own "shipping"
// log lines must not append to the batch they are shipping.

#include <string>
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

static std::string ta_log_batch;
static bool ta_log_in_sink = false;
static bool ta_log_registered = false;

static void ta_log_append(uint8_t level, const char *tag, const char *msg) {
  if (ta_log_in_sink)
    return;  // our own shipping logs do not recurse into the batch
  if (ta_log_batch.size() > 8192)
    return;  // batch full — newest lines win next tick
  static const char LEVEL_CHARS[] = {'N', 'E', 'W', 'I', 'D', 'V', 'V'};
  char lvl = (level < sizeof(LEVEL_CHARS)) ? LEVEL_CHARS[level] : '?';
  std::string line = esphome::str_sprintf("[%c][%s] %s\n", lvl, tag, msg);
  while (!line.empty() && (line.back() == '\n' || line.back() == '\r'))
    line.pop_back();
  ta_log_batch += line;
  ta_log_batch += '\n';
}

// Idempotent; called from the ship interval's first tick (loop context, so
// logger::global_logger is guaranteed set up — on_boot would be too early).
static void ta_log_register() {
  if (ta_log_registered)
    return;
  ta_log_registered = true;
  using LogCb = esphome::callback::Callback<void(uint8_t, const char *, const char *)>;
  esphome::logger::global_logger->add_on_log_callback(
      std::make_unique<LogCb>([](uint8_t level, const char *tag, const char *msg) {
        ta_log_append(level, tag, msg);
      }));
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
