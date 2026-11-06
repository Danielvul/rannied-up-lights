// The 120-LED strip outside the board, ported from ../leds.ino (the Mega version). The camera's hits replace the
// ultrasonic sensors; the sound isn't ported yet. Everything runs a step at a time from loop(), so the
// camera link never waits on the lights.

#define STRIP_PIN D2
#define LED_COUNT 120

const uint8_t BRIGHTNESS_ATTRACT = 40;
const uint8_t BRIGHTNESS_WIN = 80;

const uint8_t COLOR_RED_R = 255, COLOR_RED_G = 0, COLOR_RED_B = 0;
const uint8_t COLOR_GOLD_R = 255, COLOR_GOLD_G = 140, COLOR_GOLD_B = 0;

const unsigned long ATTRACT_UPDATE = 30;
const unsigned long WIN_UPDATE = 20;

enum AnimationState {
  STATE_ATTRACT,
  STATE_WIN_CHEERS,
  STATE_WIN_BUILDUP,
  STATE_WIN_EXPLOSION,
  STATE_WIN_CHASE,
  STATE_WIN_SPARKLE,
  STATE_WIN_FINALE
};

Adafruit_NeoPixel strip(LED_COUNT, STRIP_PIN, NEO_GRB + NEO_KHZ800);
AnimationState currentState = STATE_ATTRACT;
unsigned long lastUpdate = 0;
uint16_t animationFrame = 0;

void startStrip() {
  strip.begin();
  strip.setBrightness(BRIGHTNESS_ATTRACT);
  attractMode();  // light up straight away: the comet only starts moving after the WiFi join
}

// A hit starts the win sequence, unless one is already playing.
void stripHit() {
  if (currentState != STATE_ATTRACT) return;
  currentState = STATE_WIN_CHEERS;
  strip.setBrightness(BRIGHTNESS_WIN);
  animationFrame = 0;
}

void updateStrip() {
  unsigned long now = millis();
  if (now - lastUpdate < (currentState == STATE_ATTRACT ? ATTRACT_UPDATE : WIN_UPDATE)) return;
  lastUpdate = now;
  animationFrame++;
  switch (currentState) {
    case STATE_ATTRACT: attractMode(); break;
    case STATE_WIN_CHEERS: winCheers(); break;
    case STATE_WIN_BUILDUP: winBuildup(); break;
    case STATE_WIN_EXPLOSION: winExplosion(); break;
    case STATE_WIN_CHASE: winChase(); break;
    case STATE_WIN_SPARKLE: winSparkle(); break;
    case STATE_WIN_FINALE: winFinale(); break;
  }
}

void attractMode() {
  strip.clear();
  
  // Pulsing red comet chasing around the strip
  int cometLength = 15;
  int cometPos = (animationFrame / 2) % LED_COUNT;
  
  for (int i = 0; i < cometLength; i++) {
    int pos = (cometPos - i + LED_COUNT) % LED_COUNT;
    float brightness = (float)(cometLength - i) / cometLength;
    brightness = brightness * brightness; // Exponential falloff
    
    uint8_t r = COLOR_RED_R * brightness;
    uint8_t g = COLOR_RED_G * brightness;
    uint8_t b = COLOR_RED_B * brightness;
    
    strip.setPixelColor(pos, strip.Color(r, g, b));
  }
  
  // Add occasional gold sparkles
  if (animationFrame % 40 < 2) {
    int sparklePos = random(LED_COUNT);
    strip.setPixelColor(sparklePos, strip.Color(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B));
  }
  
  strip.show();
}

// ============================================================================
// WIN ANIMATIONS - Celebratory sequences
// ============================================================================

void winBuildup() {
  // Rapid strobing getting faster - builds anticipation
  if (animationFrame < 60) {
    int strobeSpeed = 20 - (animationFrame / 4);
    if (strobeSpeed < 3) strobeSpeed = 3;
    
    if ((animationFrame % strobeSpeed) < strobeSpeed / 2) {
      strip.fill(strip.Color(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B));
    } else {
      strip.fill(strip.Color(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B));
    }
  } else {
    // Transition to explosion
    currentState = STATE_WIN_EXPLOSION;
    animationFrame = 0;
  }
  strip.show();
}

void winExplosion() {
  // Explosion from center outward
  if (animationFrame < 60) {
    int radius = animationFrame * 2;
    strip.clear();
    
    int center = LED_COUNT / 2;
    for (int i = 0; i < radius && i < LED_COUNT / 2; i++) {
      int pos1 = center + i;
      int pos2 = center - i;
      
      float brightness = 1.0f - ((float)i / (float)max(1, radius));
      uint8_t baseR = (animationFrame % 4 < 2) ? COLOR_RED_R : COLOR_GOLD_R;
      uint8_t baseG = (animationFrame % 4 < 2) ? COLOR_RED_G : COLOR_GOLD_G;
      uint8_t baseB = (animationFrame % 4 < 2) ? COLOR_RED_B : COLOR_GOLD_B;
      uint8_t r = (uint8_t)(baseR * brightness);
      uint8_t g = (uint8_t)(baseG * brightness);
      uint8_t b = (uint8_t)(baseB * brightness);
      
      if (pos1 < LED_COUNT) strip.setPixelColor(pos1, strip.Color(r, g, b));
      if (pos2 >= 0) strip.setPixelColor(pos2, strip.Color(r, g, b));
    }
  } else {
    currentState = STATE_WIN_CHASE;
    animationFrame = 0;
  }
  strip.show();
}

void winCheers() {
  // Beer cheers animation - two glasses start close, move apart, cheers together, move apart
  strip.clear();
  
  int glassWidth = 10; // Width of each "glass"
  int center = LED_COUNT / 2;
  int separation;
  
  // Animation phases
  if (animationFrame < 20) {
    // Phase 1: Start close together
    separation = 15;
  } else if (animationFrame < 50) {
    // Phase 2: Move down/apart
    separation = 15 + (animationFrame - 20);
  } else if (animationFrame < 70) {
    // Phase 3: Quick cheers - come back together!
    int phase = animationFrame - 50;
    separation = 45 - (phase * 2);
  } else if (animationFrame < 100) {
    // Phase 4: Move apart again
    separation = 5 + (animationFrame - 70);
  } else {
    // Transition to next animation (now go to buildup)
    currentState = STATE_WIN_BUILDUP;
    animationFrame = 0;
    strip.show();
    return;
  }
  
  // During the big cheers phase, enforce a 6-LED gap between glasses at center
  if (animationFrame >= 50 && animationFrame < 70) {
    const int minGap = 6; // LEDs
    int separationMin = (glassWidth + minGap + 1) / 2; // ceil((glassWidth + gap)/2)
    if (separation < separationMin) separation = separationMin;
  }
  
  // Draw left glass (golden beer)
  int leftGlassCenter = center - separation;
  for (int i = 0; i < glassWidth; i++) {
    int pos = leftGlassCenter - glassWidth/2 + i;
    if (pos >= 0 && pos < LED_COUNT) {
      // Make the center brighter (more beer foam)
      float brightness = 1.0 - fabsf((float)(i - glassWidth/2) / (glassWidth/2)) * 0.3;
      uint8_t r = COLOR_GOLD_R * brightness;
      uint8_t g = COLOR_GOLD_G * brightness;
      uint8_t b = COLOR_GOLD_B * brightness;
      strip.setPixelColor(pos, strip.Color(r, g, b));
    }
  }
  
  // Draw right glass (golden beer)
  int rightGlassCenter = center + separation;
  for (int i = 0; i < glassWidth; i++) {
    int pos = rightGlassCenter - glassWidth/2 + i;
    if (pos >= 0 && pos < LED_COUNT) {
      // Make the center brighter (more beer foam)
      float brightness = 1.0 - fabsf((float)(i - glassWidth/2) / (glassWidth/2)) * 0.3;
      uint8_t r = COLOR_GOLD_R * brightness;
      uint8_t g = COLOR_GOLD_G * brightness;
      uint8_t b = COLOR_GOLD_B * brightness;
      strip.setPixelColor(pos, strip.Color(r, g, b));
    }
  }
  
  // Add sparkle effect during the "cheers" moment
  if (animationFrame >= 50 && animationFrame < 70) {
    // Add some sparkles when glasses clink
    for (int i = 0; i < 5; i++) {
      int sparklePos = random(LED_COUNT);
      if (random(10) < 5) {
        strip.setPixelColor(sparklePos, strip.Color(255, 255, 255)); // White sparkles
      }
    }
  }
  
  strip.show();
}

void winChase() {
  // Dual chasing lights from both ends
  if (animationFrame < 120) {
    strip.clear();
    
    int chaseWidth = 8;
    int pos1 = (animationFrame * 2) % LED_COUNT;
    int pos2 = (LED_COUNT - (animationFrame * 2)) % LED_COUNT;
    
    for (int i = 0; i < chaseWidth; i++) {
      int p1 = (pos1 + i) % LED_COUNT;
      int p2 = (pos2 - i + LED_COUNT) % LED_COUNT;
      
      uint32_t color1 = strip.Color(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B);
      uint32_t color2 = strip.Color(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B);
      
      strip.setPixelColor(p1, color1);
      strip.setPixelColor(p2, color2);
    }
  } else {
    currentState = STATE_WIN_SPARKLE;
    animationFrame = 0;
  }
  strip.show();
}

void winSparkle() {
  // Random sparkles fill up the strip
  if (animationFrame < 100) {
    // Add random sparkles
    for (int i = 0; i < 3; i++) {
      int pos = random(LED_COUNT);
      uint32_t color = (random(2) == 0) 
        ? strip.Color(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B)
        : strip.Color(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B);
      strip.setPixelColor(pos, color);
    }
    
    // Fade all pixels slightly
    if (animationFrame % 3 == 0) {
      for (int i = 0; i < LED_COUNT; i++) {
        uint32_t c = strip.getPixelColor(i);
        uint8_t r = (c >> 16) & 0xFF;
        uint8_t g = (c >> 8) & 0xFF;
        uint8_t b = c & 0xFF;
        
        r = (uint8_t)(r * 0.95f);
        g = (uint8_t)(g * 0.95f);
        b = (uint8_t)(b * 0.95f);
        
        strip.setPixelColor(i, strip.Color(r, g, b));
      }
    }
  } else {
    currentState = STATE_WIN_FINALE;
    animationFrame = 0;
  }
  strip.show();
}

void winFinale() {
  // Final wave sweep and return to attract mode
  if (animationFrame < 80) {
    strip.clear();
    
    for (int i = 0; i < LED_COUNT; i++) {
      float wave = sin(((float)i / LED_COUNT + (float)animationFrame / 40.0) * PI * 4);
      if (wave > 0) {
        uint8_t brightness = wave * 255;
        uint8_t r = (COLOR_RED_R * brightness) / 255;
        uint8_t g = (COLOR_RED_G * brightness) / 255;
        uint8_t b = (COLOR_RED_B * brightness) / 255;
        
        // Mix in gold on peaks
        if (wave > 0.7) {
          r = (COLOR_GOLD_R * brightness) / 255;
          g = (COLOR_GOLD_G * brightness) / 255;
          b = (COLOR_GOLD_B * brightness) / 255;
        }
        
        strip.setPixelColor(i, strip.Color(r, g, b));
      }
    }
  } else {
    // Return to attract mode
    currentState = STATE_ATTRACT;
    strip.setBrightness(BRIGHTNESS_ATTRACT);
    animationFrame = 0;
  }
  strip.show();
}
