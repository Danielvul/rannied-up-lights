#include <Adafruit_NeoPixel.h>
#define LED_PIN 52
#define LED_COUNT 120

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

// Track timing without delay
unsigned long lastWinTime = 0;
const unsigned long winInterval = 5000; // 5 seconds

void setup() {
 Serial.begin(115200);  // to the computer ( if connected)
    
 strip.begin(); //always needed
 backgroundLight();
 
 Serial.println("Setup complete");
}

void loop() {
  
  // Check if it's time for the next win
  unsigned long now = millis();
  if (now - lastWinTime >= winInterval) {
    win(); // trigger win simulation
    lastWinTime = millis();
  }
}

void win() {
  for(int j = 0; j < 2; j++) {
    rannyFlash();
  }

  for (int i = 0; i < 50; i++) {
    goldenVibes();
  }

  backgroundLight();
}

void multiColourVibes() {
 int n = strip.numPixels();
 // fph = FirstPixelHue
 for (long fph = 0; fph < 5 * 65536; fph += 256)
 {
 for (int i = 0; i < n; i++)
 {
 int pixelHue = fph + (i * 65536L / n);
 strip.setPixelColor(i,
 strip.gamma32(strip.ColorHSV(pixelHue))
 );
 }
 strip.show();
 //delay(1);
 }
}

void goldenVibes() {
  static uint16_t offset = 0;
  offset++;

  strip.setBrightness(60);  // 0–255 range, 40 is quite dim
  for (int i = 0; i < LED_COUNT; i++) {
    // Create a moving "blend" between red and gold along the strip
    float wave = (float)(i + offset) / LED_COUNT;  // 0 → 1 as we move
    wave = fmod(wave, 1.0);  // wrap around smoothly

    
    // Compute color at this point
    uint8_t r1 = 255, g1 = 0,   b1 = 0;   // red
    uint8_t r2 = 255, g2 = 140, b2 = 0;   // gold

    // Create a red-biased sine blend
    float blend = 0.5 + 0.5 * sin(wave * 2 * PI);
    blend = pow(blend, 2.5);  // make it linger more in red

    uint8_t r = r1 + blend * (r2 - r1);
    uint8_t g = g1 + blend * (g2 - g1);
    uint8_t b = b1 + blend * (b2 - b1);

    strip.setPixelColor(i, strip.Color(r, g, b));
  }
  
  //strip.setBrightness(80 + 40 * sin(offset * 0.1));
  strip.show();
}

void rannyFlash() {
   strip.setBrightness(60);  // 0–255 range, 40 is quite dim
   
  // Colors
  uint8_t r1 = 255, g1 = 0,   b1 = 0;    // red
  uint8_t r2 = 255, g2 = 140, b2 = 0;    // gold-orange

  // Flash red
  strip.fill(strip.Color(r1, g1, b1));
  strip.show();
  delay(150);

  // Quick blackout for contrast
  strip.clear();
  strip.show();
  delay(80);

  // Flash gold
  strip.fill(strip.Color(r2, g2, b2));
  strip.show();
  delay(150);

  // Quick blackout again
  strip.clear();
  strip.show();
  delay(80);

  // Small pause between cycles
  delay(150);
}

void backgroundLight() {

  // A warm red-gold mix — mostly red, with a hint of amber
  uint8_t r = 255;   // strong base of red
  uint8_t g = 0;   // just enough green to add warmth
  uint8_t b = 0;    // no blue for a pure fire tone

  // Optionally, you can globally dim everything further:
  strip.setBrightness(10);  // 0–255 range, 40 is quite dim

  // Fill the strip
  strip.fill(strip.Color(r, g, b));
  strip.show();
}
