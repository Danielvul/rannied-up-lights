/*
  WiFi <-> camera bridge for the Grove Vision AI V2, running on a XIAO ESP32-C3
  stacked on the V2 (its socket joins the XIAO's pins to the V2's serial and SPI ports).

  This sketch passes bytes straight between the V2 and one TCP client, so viewer.py
  and capture.py work over WiFi exactly as they do over USB:

      .venv/bin/python viewer.py --port socket://boardcam.local:3333

  Link to the V2 (LINK_SPI below):
    SPI (default): SCK D8, MISO D9, MOSI D10, select D1, data-ready D0. Needs the
      OpenCorn V2 firmware (v2_firmware/, fast-640.img), which turns the V2's SPI port
      on. Room for 45 photos a second.
    Serial: V2 TX -> D7, D6 -> V2 RX, 921600 baud. Works with Seeed's stock firmware
      but tops out near 21 photos a second.

  WiFi: put your network in secrets.h (copy secrets.example.h). If WIFI_SSID is
  empty or the network can't be reached within 15 s, the XIAO starts its own
  network called "BoardCam" instead (password AP_PASSWORD), at 192.168.4.1.
*/

#include <Adafruit_NeoPixel.h>
#include <ESPmDNS.h>
#include <SPI.h>
#include <WiFi.h>
#include <errno.h>
#include <lwip/sockets.h>

#include "secrets.h"

#define LINK_SPI 0       // 1 = SPI link (OpenCorn V2 firmware). SPI isn't answering yet, see SPI_PROBE
#define SPI_PROBE 0      // 1 = at boot, test the SPI link and print the results on USB. Leave 0 with the
                         // serial link: junk on the V2's SPI pins can hang it when its SPI port is off
#define TCP_PORT 3333
#define HOSTNAME "boardcam"
#define RING_PIN D0        // XC4385 ring DIN. D0 is the SPI ready pin, so the ring only works with LINK_SPI 0
#define RING_LEDS 24
#define RING_BRIGHTNESS 76  // of 255, about 30%: full white pulls about 1.4 A
#define V2_ASK_MS 1500      // nothing from the V2 for this long: ask if it's there
#define V2_LOST_MS 5000     // still nothing: the ring turns red
#define HIT_SCORE 70        // the V2 only reports dots at least this sure, out of 100
#define HIT_FRAMES 1        // frames in a row with a dot before it counts as a hit
#define HIT_COOLDOWN_MS 3000  // one hit per bag: ignore dots for this long after a hit
#define HIT_SHOW_MS 1000

Adafruit_NeoPixel ring(RING_LEDS, RING_PIN, NEO_GRB + NEO_KHZ800);
WiFiServer server(TCP_PORT);
WiFiClient client;
uint8_t up[4096];      // camera -> laptop, waiting to be sent
size_t upLen = 0, upOff = 0;
uint32_t lastProgress = 0;  // last time the laptop took data (or there was nothing to send)
uint8_t down[1024];    // laptop -> camera
uint32_t bytesFromV2 = 0, bytesToV2 = 0;  // shown on USB every 5 s, for wiring checks
volatile uint32_t linkErrors = 0;          // serial: bytes lost because WiFi fell behind the camera

#if LINK_SPI || SPI_PROBE
// The V2's SPI transport (SSCMA-Micro el_sspi_we2.cpp): every command is a 256-byte packet
// [0x10, cmd, len hi, len lo, data..., 0xFF, 0xFF]; the V2 answers in the next transfer.
const int CS_PIN = D1;
const int READY_PIN = D0;          // high while the V2 has output waiting
// Clock, pause and mode are found at boot by probeSpi(): the first combination that gets a real
// reply to AT+ID? wins (Seeed's library uses 15 MHz, 2 ms, mode 0 for this link).
uint32_t SPI_HZ = 15000000;
uint32_t SETTLE_US = 2000;         // lets the V2 re-arm its SPI DMA between transfers
uint8_t SPI_MODE = SPI_MODE0;
const size_t V2_TX_BUFFER = 32768; // the V2's SPI send buffer; a bigger "available" is line noise
enum : uint8_t { CMD_READ = 0x01, CMD_WRITE = 0x02, CMD_AVAILABLE = 0x03, CMD_RESET = 0x06 };
uint8_t packet[256];

void spiCmd(uint8_t cmd, uint16_t len, const uint8_t *data) {
  memset(packet, 0, sizeof(packet));
  packet[0] = 0x10;
  packet[1] = cmd;
  packet[2] = len >> 8;
  packet[3] = len & 0xff;
  if (data) memcpy(packet + 4, data, len);
  packet[4 + (data ? len : 0)] = 0xff;
  packet[5 + (data ? len : 0)] = 0xff;
  delayMicroseconds(SETTLE_US);
  digitalWrite(CS_PIN, LOW);
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE));
  SPI.transfer(packet, sizeof(packet));
  SPI.endTransaction();
  digitalWrite(CS_PIN, HIGH);
  delayMicroseconds(SETTLE_US);
}

void spiFetch(uint8_t *buf, size_t n) {  // clock out the V2's answer to the last command
  digitalWrite(CS_PIN, LOW);
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE));
  SPI.transfer(buf, n);
  SPI.endTransaction();
  digitalWrite(CS_PIN, HIGH);
}

size_t spiAvailable() {
  if (digitalRead(READY_PIN) == LOW) return 0;
  uint8_t n[2] = {0xff, 0xff};
  spiCmd(CMD_AVAILABLE, 0, nullptr);
  spiFetch(n, 2);
  size_t avail = (n[0] << 8) | n[1];
  if (avail > V2_TX_BUFFER) {  // all ones: nothing is driving the line
    linkErrors++;
    return 0;
  }
  return avail;
}

size_t spiRead(uint8_t *buf, size_t max) {
  size_t n = min(spiAvailable(), min(max, (size_t)4095));
  if (!n) return 0;
  spiCmd(CMD_READ, n, nullptr);
  spiFetch(buf, n);
  return n;
}

void spiWrite(const uint8_t *data, size_t n) {
  while (n) {
    size_t k = min(n, (size_t)250);  // command data must fit one 256-byte packet
    spiCmd(CMD_WRITE, k, data);
    data += k;
    n -= k;
  }
}

void spiReset() { spiCmd(CMD_RESET, 0, nullptr); }  // the V2 empties its SPI buffers

// Ask for the V2's ID with one clock/pause/mode combination; true if a JSON reply comes back.
bool spiTry(uint32_t hz, uint32_t settle, uint8_t mode) {
  SPI_HZ = hz;
  SETTLE_US = settle;
  SPI_MODE = mode;
  spiReset();
  delay(20);
  const char *cmd = "AT+ID?\r";
  spiWrite((const uint8_t *)cmd, strlen(cmd));
  uint8_t reply[300];
  size_t got = 0;
  for (uint32_t t = millis(); millis() - t < 300 && got < 40;) {
    got += spiRead(reply + got, sizeof(reply) - 1 - got);
    delay(5);
  }
  reply[got] = 0;
  bool ok = got && strstr((char *)reply, "\"ID?\"");
  Serial.printf("  SPI %2u MHz, pause %4u us, mode %u, ready pin %s: %s (%u bytes)\n", hz / 1000000, settle, mode,
                digitalRead(READY_PIN) ? "high" : "low", ok ? "OK" : "no reply", got);
  return ok;
}

// Step-by-step SPI check, printed on USB: does each command reach the V2, and what comes back?
void spiDiagnose(uint32_t hz) {
  SPI_HZ = hz;
  SETTLE_US = 2000;
  SPI_MODE = SPI_MODE0;
  Serial.printf(" diag %u kHz: ready %d", hz / 1000, digitalRead(READY_PIN));
  spiCmd(CMD_RESET, 0, nullptr);
  delay(50);
  Serial.printf(" | after RESET %d", digitalRead(READY_PIN));
  const char *cmd = "AT+ID?\r";
  spiCmd(CMD_WRITE, strlen(cmd), (const uint8_t *)cmd);
  delay(200);
  Serial.printf(" | after AT+ID? %d", digitalRead(READY_PIN));
  for (uint32_t wait : {2u, 10u}) {
    spiCmd(CMD_AVAILABLE, 0, nullptr);
    delay(wait);
    uint8_t raw[6];
    memset(raw, 0xaa, sizeof(raw));
    spiFetch(raw, sizeof(raw));
    Serial.printf(" | AVAILABLE +%ums: %02x %02x %02x %02x %02x %02x", wait, raw[0], raw[1], raw[2], raw[3], raw[4], raw[5]);
  }
  spiCmd(CMD_READ, 32, nullptr);
  delay(5);
  uint8_t data[33] = {0};
  spiFetch(data, 32);
  Serial.print(" | READ 32: ");
  for (int i = 0; i < 32; i++) Serial.print(isprint(data[i]) ? (char)data[i] : '.');
  Serial.println();
}

void probeSpi() {
  pinMode(CS_PIN, OUTPUT);
  digitalWrite(CS_PIN, HIGH);
  pinMode(READY_PIN, INPUT);
  Serial.printf("SPI probe (needs the OpenCorn V2 firmware). Ready pin idles %s:\n",
                digitalRead(READY_PIN) ? "high" : "low");
  SPI.begin(D8, D9, D10, -1);
  for (uint32_t hz : {500000u, 1000000u, 2000000u, 3000000u}) spiDiagnose(hz);
  const uint32_t clocks[] = {15000000, 4000000, 1000000};
  const uint8_t modes[] = {SPI_MODE0, SPI_MODE3};
  const int8_t pins[][3] = {{D8, D9, D10}, {D8, D10, D9}};  // {SCK, MISO, MOSI}: as Seeed's example, then data lines swapped
  for (auto &p : pins) {
    SPI.end();
    SPI.begin(p[0], p[1], p[2], -1);
    Serial.printf(" SCK D%d, MISO D%d, MOSI D%d:\n", p[0] == D8 ? 8 : 0, p[1] == D9 ? 9 : 10, p[2] == D9 ? 9 : 10);
    for (uint8_t mode : modes)
      for (uint32_t hz : clocks)
        if (spiTry(hz, 2000, mode)) {
          Serial.printf("Using SPI %u MHz, mode %u, MISO D%d\n", hz / 1000000, mode, p[1] == D9 ? 9 : 10);
          return;
        }
  }
  SPI.end();
  SPI.begin(D8, D9, D10, -1);
  SPI_HZ = 4000000, SETTLE_US = 2000, SPI_MODE = SPI_MODE0;
  Serial.println("No SPI reply from the V2: is fast-640.img (with grove-spi.patch) flashed?");
}

#endif

#if LINK_SPI
size_t linkAvailable() { return spiAvailable(); }
size_t linkRead(uint8_t *buf, size_t max) { return spiRead(buf, max); }
void linkWrite(const uint8_t *data, size_t n) { spiWrite(data, n); }
void linkReset() { spiReset(); }
void startLink() { probeSpi(); }

#else
#define V2_BAUD 921600   // the V2 firmware's UART speed
HardwareSerial V2(1);    // UART1, on the pins below
const int RX_PIN = D7;   // V2 TX (PB7) -> XIAO D7, via the stacking socket
const int TX_PIN = D6;   // XIAO D6 -> V2 RX (PB6), via the stacking socket

void onUartError(hardwareSerial_error_t err) {
  if (err == UART_BUFFER_FULL_ERROR || err == UART_FIFO_OVF_ERROR) linkErrors++;
}

// Wiring check: the V2's TXD is an output that idles HIGH, its RXD is an input.
// With weak pull-downs on both XIAO pins, only the one wired to TXD reads high.
void probeWiring() {
  pinMode(RX_PIN, INPUT_PULLDOWN);
  pinMode(TX_PIN, INPUT_PULLDOWN);
  delay(20);
  int rx = digitalRead(RX_PIN), tx = digitalRead(TX_PIN);
  Serial.printf("Wiring check: RX pin=%s TX pin=%s -> ", rx ? "HIGH" : "low", tx ? "HIGH" : "low");
  if (rx && !tx) Serial.println("looks right (V2 TXD on the RX pin)");
  else if (!rx && tx) Serial.println("SWAPPED: V2 TXD is on the TX pin. Swap the two data wires.");
  else if (!rx && !tx) Serial.println("no signal on either pin: data wires not connected, or V2 not powered/booted");
  else Serial.println("both high: can't tell (V2 may pull its RXD up)");
}

size_t linkAvailable() { return V2.available(); }
size_t linkRead(uint8_t *buf, size_t max) { return V2.readBytes(buf, min((size_t)V2.available(), max)); }
void linkWrite(const uint8_t *data, size_t n) { V2.write(data, n); }

// Drop what's already buffered (stale), in bulk. Never "read until empty": while the camera
// streams, data arrives about as fast as single-byte reads remove it and that loop never ends.
void linkReset() {
  for (size_t stale = V2.available(); stale > 0;) {
    size_t k = V2.readBytes(up, min(stale, sizeof(up)));
    if (!k) break;
    stale -= k;
  }
}

void startLink() {
#if SPI_PROBE
  probeSpi();
#endif
  probeWiring();
  V2.setRxBufferSize(98304);  // ~1.2 s of camera data: rides out WiFi delay spikes, then catches up
  V2.begin(V2_BAUD, SERIAL_8N1, RX_PIN, TX_PIN);
  V2.onReceiveError(onUartError);
  Serial.println("Link: serial to the V2 at 921600 baud");
}
#endif

void startWifi() {
  WiFi.setHostname(HOSTNAME);
  if (strlen(WIFI_SSID) > 0) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    WiFi.setSleep(false);  // power saving adds big latency spikes
    Serial.printf("Joining %s", WIFI_SSID);
    for (int i = 0; i < 30 && WiFi.status() != WL_CONNECTED; i++) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("Connected. IP %s\n", WiFi.localIP().toString().c_str());
      return;
    }
    Serial.println("Couldn't join, starting own network instead.");
  }
  WiFi.mode(WIFI_AP);
  WiFi.softAP("BoardCam", AP_PASSWORD);
  WiFi.setSleep(false);
  Serial.printf("Own network \"BoardCam\" up. IP %s\n", WiFi.softAPIP().toString().c_str());
}

// Restart the V2 so both boards start clean (D3 is the V2's reset line, as in Seeed's examples).
void resetV2() {
  pinMode(D3, OUTPUT);
  digitalWrite(D3, LOW);
  delay(50);
  pinMode(D3, INPUT);  // release: the V2 holds its own reset high
}

// The ring is the camera's status light: white lights the bag at night, red means the ESP can't reach the V2.
void ringColor(uint8_t r, uint8_t g, uint8_t b) {
  ring.fill(ring.Color(r, g, b));
  ring.show();
}

void startRing() {
  ring.begin();
  ring.setBrightness(RING_BRIGHTNESS);
  ringColor(255, 0, 0);
}

bool v2Seen = false;       // the V2 is answering
uint32_t showUntil = 0;    // hit light show running until this time

void ringStatus() {
  if (v2Seen) ringColor(255, 255, 255);
  else ringColor(255, 0, 0);
}

// Streaming keeps the bytes coming; when it's quiet, a harmless question shows if the V2 is still there.
void watchV2(size_t n) {
  static const char ask[] = "AT+ID?\r\n";
  static uint32_t lastFromV2 = 0, lastAsk = 0;
  uint32_t now = millis();
  if (n >= 8) lastFromV2 = now;  // real replies are longer: a reset can leave a stray byte on the line
  if (upOff < upLen) return;     // network busy, so nothing new was read: can't tell yet
  bool alive = lastFromV2 && now - lastFromV2 < V2_LOST_MS;
  if (alive != v2Seen) {
    v2Seen = alive;
    Serial.println(alive ? "V2 found: ring white" : "V2 lost: ring red");
    if (!showUntil) ringStatus();
  }
  if (now - lastFromV2 > V2_ASK_MS && now - lastAsk > V2_ASK_MS) {
    lastAsk = now;
    linkWrite((const uint8_t *)ask, sizeof(ask) - 1);
    bytesToV2 += sizeof(ask) - 1;
  }
}

// With no laptop connected, the V2 runs the model and sends results only: its full 45 fps.
void startGame(bool laptop) {
  static const char go[] = "AT+BREAK\r\n";
  static bool running = false;
  static uint32_t quietSince = 0;
  char cmd[48];
  uint32_t now = millis();
  if (laptop || !v2Seen) {
    running = false;
    quietSince = now;
    return;
  }
  if (running || now - quietSince < 2000) return;
  running = true;
  linkWrite((const uint8_t *)go, sizeof(go) - 1);
  delay(100);
  size_t len = snprintf(cmd, sizeof(cmd), "AT+INVOKE=-1,0,1\r\n");
  linkWrite((const uint8_t *)cmd, len);
  delay(1000);  // the V2 only knows TSCORE while the model is running
  len = snprintf(cmd, sizeof(cmd), "AT+TSCORE=%d\r\n", HIT_SCORE);
  linkWrite((const uint8_t *)cmd, len);
  Serial.println("Game mode: V2 running the model");
}

void frameResult(bool dot) {
  static uint8_t inARow = 0;
  static uint32_t lastHit = 0;
  uint32_t now = millis();
  inARow = dot ? inARow + 1 : 0;
  if (inARow < HIT_FRAMES || (lastHit && now - lastHit < HIT_COOLDOWN_MS)) return;
  lastHit = now;
  inARow = 0;
  showUntil = now + HIT_SHOW_MS;
  stripHit();
  Serial.println("HIT");
}

// Each V2 result has "boxes": [] or "boxes": [[x, y, w, h, score, class]]. Base64 photos never contain a quote.
void scanHits(const uint8_t *data, size_t n) {
  static const char key[] = "\"boxes\":";
  static uint8_t matched = 0, brackets = 0;
  for (size_t i = 0; i < n; i++) {
    char c = data[i];
    if (matched < sizeof(key) - 1) {
      matched = c == key[matched] ? matched + 1 : c == key[0];
      continue;
    }
    if (c == ' ') continue;
    if (!brackets && c == '[') {
      brackets = 1;
      continue;
    }
    if (brackets) frameResult(c == '[');
    matched = brackets = 0;
  }
}

// Gold flashes chasing round the ring, then back to the status colour.
void hitShow() {
  static uint32_t next = 0;
  static uint16_t step = 0;
  if (!showUntil) return;
  uint32_t now = millis();
  if (now >= showUntil) {
    showUntil = 0;
    ringStatus();
    return;
  }
  if (now < next) return;
  next = now + 30;
  step++;
  ring.fill(step & 2 ? ring.Color(255, 160, 0) : ring.Color(0, 0, 0));
  for (int i = 0; i < 6; i++) ring.setPixelColor((step + i * RING_LEDS / 6) % RING_LEDS, ring.Color(255, 255, 255));
  ring.show();
}

void setup() {
  Serial.begin(115200);  // USB, for status messages only
  resetV2();
  Serial.setTxTimeoutMs(0);  // never wait on USB: if nothing reads it, a print used to stall the bridge ~2 s
  delay(500);
  startRing();  // red until the V2 answers
  startStrip();
  startWifi();  // before the link: joining WiFi takes seconds and the camera may still be streaming
  startLink();
  MDNS.begin(HOSTNAME);
  MDNS.addService("boardcam", "tcp", TCP_PORT);
  server.begin();
  server.setNoDelay(true);
  Serial.printf("Bridge ready on port %d (socket://%s.local:%d), free memory %u bytes\n", TCP_PORT, HOSTNAME, TCP_PORT, ESP.getFreeHeap());
}

void dropClient(const char *why) {
  Serial.printf("Dropping client: %s\n", why);
  client.stop();
  upLen = upOff = 0;
}

void loop() {
  // One client at a time; a new connection replaces the old one.
  if (server.hasClient()) {
    if (client) client.stop();
    client = server.accept();
    client.setNoDelay(true);
    linkReset();                        // drop stale camera output
    upLen = upOff = 0;
    linkErrors = 0;                     // count losses per connection
    lastProgress = millis();
    Serial.printf("Client connected from %s\n", client.remoteIP().toString().c_str());
  }
  bool connected = client && client.connected();

  // Camera -> laptop. Never block: send what the network takes now, keep the rest for the next
  // pass, and only read more from the camera once it's all gone (the V2's buffer absorbs the wait).
  size_t n = 0;
  if (upOff >= upLen) {
    upLen = upOff = 0;
    n = linkRead(up, sizeof(up));  // always drain, so the V2 never stalls on a full buffer
    bytesFromV2 += n;
    scanHits(up, n);
    if (connected) upLen = n;      // no client: discard
  }
  if (connected && upOff < upLen) {
    int sent = send(client.fd(), up + upOff, upLen - upOff, MSG_DONTWAIT);
    if (sent > 0) {
      upOff += sent;
      lastProgress = millis();
    } else if (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
      dropClient("connection error");
    } else if (millis() - lastProgress > 5000) {
      dropClient("not reading for 5 s");  // e.g. a viewer that vanished without closing
    }
  } else {
    lastProgress = millis();
  }

  // Laptop -> camera (commands; small).
  if (client && client.connected()) {
    size_t m = client.available();
    if (m) {
      m = client.read(down, min(m, sizeof(down)));
      linkWrite(down, m);
      bytesToV2 += m;
    }
  }

  watchV2(n);
  startGame(client && client.connected());
  hitShow();
  updateStrip();

  // Station mode: reconnect if the router drops us.
  if (WiFi.getMode() == WIFI_STA && WiFi.status() != WL_CONNECTED) {
    static uint32_t lastTry = 0;
    if (millis() - lastTry > 5000) {
      lastTry = millis();
      WiFi.reconnect();
    }
  }
  static uint32_t lastReport = 0;
  if (millis() - lastReport > 5000) {
    lastReport = millis();
    Serial.printf("bytes to V2: %u, from V2: %u, link errors: %u%s\n", bytesToV2, bytesFromV2, linkErrors,
                  bytesToV2 && !bytesFromV2 ? "  <- V2 not answering: check the link and that the V2 firmware matches LINK_SPI" : "");
  }
  // Pause whenever there's nothing new or the network is busy: never spin without yielding,
  // or the chip's background tasks starve and the bridge locks up.
  if (!n || upOff < upLen) delay(1);
}
