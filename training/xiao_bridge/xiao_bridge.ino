/*
  WiFi <-> serial bridge for the Grove Vision AI V2, running on a XIAO ESP32-C3
  stacked on the V2 (its socket joins the XIAO's D6/D7 to the V2's UART).

  Stacked: V2 TX -> XIAO D7, XIAO D6 -> V2 RX; 5V, 3V3 and GND are shared through the socket.
  This sketch passes bytes straight between that UART and one TCP client, so
  viewer.py and capture.py work over WiFi exactly as they do over USB:

      .venv/bin/python viewer.py --port socket://boardcam.local:3333

  WiFi: put your network in secrets.h (copy secrets.example.h). If WIFI_SSID is
  empty or the network can't be reached within 15 s, the XIAO starts its own
  network called "BoardCam" instead (password AP_PASSWORD), at 192.168.4.1.
*/

#include <ESPmDNS.h>
#include <WiFi.h>
#include <errno.h>
#include <lwip/sockets.h>

#include "secrets.h"

#define V2_BAUD 921600   // the V2 firmware's UART speed
#define TCP_PORT 3333
#define HOSTNAME "boardcam"

HardwareSerial V2(1);    // UART1, on the pins below
const int RX_PIN = D7;   // V2 TX (PB7) -> XIAO D7, via the stacking socket
const int TX_PIN = D6;   // XIAO D6 -> V2 RX (PB6), via the stacking socket

WiFiServer server(TCP_PORT);
WiFiClient client;
uint8_t up[4096];      // camera -> laptop, waiting to be sent
size_t upLen = 0, upOff = 0;
uint32_t lastProgress = 0;  // last time the laptop took data (or there was nothing to send)
uint8_t down[1024];    // laptop -> camera
uint32_t bytesFromV2 = 0, bytesToV2 = 0;  // shown on USB every 5 s, for wiring checks
volatile uint32_t uartOverflows = 0;       // bytes lost because WiFi fell behind the camera

void onUartError(hardwareSerial_error_t err) {
  if (err == UART_BUFFER_FULL_ERROR || err == UART_FIFO_OVF_ERROR) uartOverflows++;
}

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

void setup() {
  Serial.begin(115200);  // USB, for status messages only
  Serial.setTxTimeoutMs(0);  // never wait on USB: if nothing reads it, a print used to stall the bridge ~2 s
  delay(500);
  probeWiring();
  startWifi();  // before the UART: joining WiFi takes seconds and the camera may still be streaming
  V2.setRxBufferSize(98304);  // ~1.2 s of camera data: rides out WiFi delay spikes, then catches up
  V2.begin(V2_BAUD, SERIAL_8N1, RX_PIN, TX_PIN);
  V2.onReceiveError(onUartError);
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
    // Drop what's already buffered (stale), in bulk. Never "read until empty": while the camera
    // streams, data arrives about as fast as single-byte reads remove it and that loop never ends.
    for (size_t stale = V2.available(); stale > 0;) {
      size_t k = V2.readBytes(up, min(stale, sizeof(up)));
      if (!k) break;
      stale -= k;
    }
    upLen = upOff = 0;
    uartOverflows = 0;                  // count losses per connection
    lastProgress = millis();
    Serial.printf("Client connected from %s\n", client.remoteIP().toString().c_str());
  }
  bool connected = client && client.connected();

  // Camera -> laptop. Never block: send what the network takes now, keep the rest for the next
  // pass, and only read more from the camera once it's all gone (the UART buffer absorbs the wait).
  size_t n = 0;
  if (upOff >= upLen) {
    upLen = upOff = 0;
    n = V2.available();
    if (n) {
      n = V2.readBytes(up, min(n, sizeof(up)));
      bytesFromV2 += n;
      if (connected) upLen = n;  // no client: discard
    }
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
      V2.write(down, m);
      bytesToV2 += m;
    }
  }

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
    Serial.printf("bytes to V2: %u, from V2: %u, overflows: %u%s\n", bytesToV2, bytesFromV2, uartOverflows,
                  bytesToV2 && !bytesFromV2 ? "  <- V2 not answering: check TXD/RXD wires, 5V and GND" : "");
  }
  // Pause whenever there's nothing new or the network is busy: never spin without yielding,
  // or the chip's background tasks starve and the bridge locks up.
  if (!n || upOff < upLen) delay(1);
}
