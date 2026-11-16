#pragma once
// Lyrics word-wrap helper for the lyrics view (0.4.29).
// Splits a lyric line into centered rows of <=15 chars (3x5 font capacity,
// 16 max but 15 leaves room for the drop shadow). Firmware lambda mirrors
// the emulator's wrap so the two stay pixel-identical.
#include <string>
#include <vector>

static inline std::vector<std::string> lyrics_wrap(const std::string &text,
                                                   int width = 15) {
  std::vector<std::string> rows;
  std::string cur;
  size_t start = 0;
  while (start <= text.size()) {
    size_t sp = text.find(' ', start);
    std::string w = (sp == std::string::npos)
                        ? text.substr(start)
                        : text.substr(start, sp - start);
    if (!w.empty()) {
      std::string cand = cur.empty() ? w : cur + " " + w;
      if ((int) cand.size() <= width) {
        cur = cand;
      } else {
        if (!cur.empty()) rows.push_back(cur);
        while (w.size() > (size_t) width) {       // gigantic word: hard split
          rows.push_back(w.substr(0, width));
          w = w.substr(width);
        }
        cur = w;
      }
    }
    if (sp == std::string::npos) break;
    start = sp + 1;
  }
  if (!cur.empty()) rows.push_back(cur);
  if (rows.size() > 4) {                          // impossible with Apple
    rows.resize(4);                               // lines, but never overflow
    rows[3] = rows[3].substr(0, width - 1) + ".";
  }
  return rows;
}
