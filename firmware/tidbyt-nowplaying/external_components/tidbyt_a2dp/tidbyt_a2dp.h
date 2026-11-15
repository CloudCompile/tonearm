#pragma once

#include <Arduino.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <BluetoothA2DPSource.h>
#include <deque>
#include <mutex>
#include <string>
#include "esphome/core/component.h"
#include "esphome/core/log.h"

namespace tidbyt_a2dp {

class TidbytA2DPSource : public esphome::Component {
 public:
  TidbytA2DPSource(const std::string &device_name, const std::string &url)
      : device_name_(device_name), url_(url) {}

  void setup() override;
  void loop() override;
  float get_setup_priority() const override { return esphome::setup_priority::AFTER_WIFI; }
  void dump_config() override;
  bool is_deferred() const { return deferred_; }
  bool is_started() const { return started_; }
  unsigned internal_heap() const {
    return heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  }
  unsigned largest_internal_block() const {
    return heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  }

 protected:
  static TidbytA2DPSource *instance_;
  static int32_t audio_callback(uint8_t *data, int32_t bytes);
  static void stream_task(void *arg);
  void stream_loop();
  bool connect_stream(WiFiClient &client);
  int read_stream_bytes(WiFiClient &client, uint8_t *buffer, size_t capacity);

  // BTU needs internal DRAM and a contiguous block; total free heap includes
  // PSRAM and is not a valid measure for this preflight.
  static constexpr unsigned kMinHeapForBt = 180 * 1024;
  BluetoothA2DPSource source_;
  std::string device_name_;
  std::string url_;
  std::deque<uint8_t> pcm_;
  std::mutex mutex_;
  TaskHandle_t task_{nullptr};
  bool chunked_{false};
  size_t chunk_remaining_{0};
  unsigned long boot_ms_{0};
  bool started_{false};
  bool failed_{false};
  bool deferred_{false};
};

}  // namespace tidbyt_a2dp
