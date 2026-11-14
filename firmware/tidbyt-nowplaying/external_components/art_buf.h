#pragma once
// Shared between the artwork-fetch lambda and the display lambda
// (both live in the generated main.cpp — single translation unit).
// Backdrop: 64x32 RGB565 big-endian, blurred + dimmed by the server.
static uint8_t bd_buf[64 * 32 * 2];
static bool bd_have = false;
