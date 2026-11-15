#pragma once
// Shared between the artwork-fetch lambda and the display lambda
// (both live in the generated main.cpp — single translation unit).
// Backdrop: 64x32 RGB565 big-endian, blurred + dimmed by the server.
static uint8_t bd_buf[64 * 32 * 2];
static bool bd_have = false;

// Internal-DRAM telemetry helpers (v0.4.8: no BT component anymore, but the
// heap numbers are how we know whether audio/display can coexist).
#include <esp_heap_caps.h>
static inline unsigned ta_internal_heap() {
  return heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}
static inline unsigned ta_internal_largest() {
  return heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}
// DMA-capable internal DRAM — what I2S DMA buffers come out of. Audio must
// never be attempted when this is small; the legacy I2S driver fails its
// allocation and the media player then dereferences the dead handle
// (LoadProhibited crash, seen in v0.4.6).
static inline unsigned ta_dma_heap() {
  return heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT | MALLOC_CAP_DMA);
}
