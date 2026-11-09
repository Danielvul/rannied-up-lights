// The 120-LED strip outside the board, ported from ../leds.ino (the Mega version). The camera's hits replace the
// ultrasonic sensors, and the sound is in sound.ino. Everything runs a step at a time from loop(), so the
// camera link never waits on the lights.

#define STRIP_PIN D2
#define LED_COUNT 120

const uint8_t BRIGHTNESS_ATTRACT = 40;
const uint8_t BRIGHTNESS_WIN = 80;

const uint8_t COLOR_RED_R = 255, COLOR_RED_G = 0, COLOR_RED_B = 0;
const uint8_t COLOR_GOLD_R = 255, COLOR_GOLD_G = 140, COLOR_GOLD_B = 0;
const uint8_t COLOR_FOAM_R = 255, COLOR_FOAM_G = 235, COLOR_FOAM_B = 190;  // creamy beer head
const uint8_t FOAM_LEDS = 3;

const unsigned long ATTRACT_UPDATE = 30;
const unsigned long WIN_UPDATE = 20;
const uint8_t FADE_FRAMES = 15;  // 0.3 s between scenes

enum AnimationState {
  STATE_ATTRACT,
  STATE_WIN_CHEERS,
  STATE_WIN_BUILDUP,
  STATE_WIN_EXPLOSION,
  STATE_WIN_CHASE,
  STATE_WIN_SPARKLE,
  STATE_FADE
};

Adafruit_NeoPixel strip(LED_COUNT, STRIP_PIN, NEO_GRB + NEO_KHZ800);
AnimationState currentState = STATE_ATTRACT;
AnimationState nextState = STATE_ATTRACT;  // where STATE_FADE goes when it's done
unsigned long lastUpdate = 0;
uint16_t animationFrame = 0;
bool buildupGold = false;  // the strobe's colour now: the ring copies it
uint8_t framesSinceSwap = 0;

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
    case STATE_FADE: fadeStep(); break;
  }
  if (currentState != STATE_ATTRACT) ringScene();
}

bool stripIdle() {
  return currentState == STATE_ATTRACT;
}

// The ring matches every win scene. It sits out of sight under the board and only its glow shows, so it uses
// whole-ring colours and flashes: nothing that moves around the ring.
void ringScene() {
  uint16_t f = animationFrame;
  uint8_t level;
  switch (currentState) {
    case STATE_WIN_CHEERS:  // gold builds, flashes as the glasses meet, red as they bounce apart
      level = 64 + 191 * min((int)f, 50) / 50;
      if (f < 50) ringColor(level, level * COLOR_GOLD_G / 255, 0);
      else if (f < 70) ((f - 50) / 4 % 2) ? ringColor(0, 0, 0) : ringColor(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B);
      else ringColor(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B);
      break;
    case STATE_WIN_BUILDUP:  // the strip's own red and gold strobe
      if (buildupGold) ringColor(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B);
      else ringColor(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B);
      break;
    case STATE_WIN_EXPLOSION:  // one flash per blast: gold, then red, each dying away
      if (f < 20) {
        level = 255 * (20 - f) / 20;
        ringColor(level, level * COLOR_GOLD_G / 255, 0);
      } else {
        ringColor(255 * (60 - min((int)f, 60)) / 40, 0, 0);
      }
      break;
    case STATE_WIN_CHASE:  // swaps colour as the two lights run
      if (f / 15 % 2) ringColor(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B);
      else ringColor(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B);
      break;
    case STATE_WIN_SPARKLE:  // a flicker of random brightness, red or gold
      if (f % 3 == 0) {
        level = random(40, 256);
        ringColor(level, random(2) ? level * COLOR_GOLD_G / 255 : 0, 0);
      }
      break;
    case STATE_FADE: {  // dims with the strip between scenes
      uint8_t* pixels = ring.getPixels();
      for (int i = 0; i < RING_LEDS * 3; i++) pixels[i] = pixels[i] * 220 / 256;
      ring.show();
      break;
    }
    default:
      break;
  }
}

void attractMode() {
  strip.clear();
  
  // Red comet chasing around the strip. Its head sits between LEDs, shared across the two, so it glides.
  const int cometLength = 15;
  float head = fmodf(animationFrame * 0.5f, LED_COUNT);
  int first = (int)head;

  for (int i = -1; i < cometLength; i++) {
    int pos = (first - i + LED_COUNT) % LED_COUNT;
    float distance = head - (first - i);  // how far this LED is behind the head
    float brightness;
    if (distance < 0) {
      brightness = 1.0f + distance;  // the LED the head is moving onto
    } else {
      brightness = 1.0f - distance / cometLength;
      brightness = brightness * brightness;  // Exponential falloff
    }

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

void fadeTo(int next) {  // an int: Arduino writes this function's prototype above the enum
  nextState = (AnimationState)next;
  currentState = STATE_FADE;
  animationFrame = 0;
}

// Dims what's on the strip, then starts the next scene. Works on the raw pixels, so colours stay true as they fade.
void fadeStep() {
  uint8_t* pixels = strip.getPixels();
  for (int i = 0; i < LED_COUNT * 3; i++) pixels[i] = pixels[i] * 220 / 256;
  if (animationFrame >= FADE_FRAMES) {
    strip.clear();
    currentState = nextState;
    animationFrame = 0;
    if (nextState == STATE_ATTRACT) {
      strip.setBrightness(BRIGHTNESS_ATTRACT);
      stopSong();
      ringStatus();
    }
  }
  strip.show();
}

void winBuildup() {
  if (animationFrame == 1) startSong(JACKPOT_BUILDUP);
  // Rapid strobing getting faster - builds anticipation
  if (animationFrame < 60) {
    int strobeSpeed = 20 - (animationFrame / 4);
    if (strobeSpeed < 3) strobeSpeed = 3;

    // Count frames since the last swap, so each flash is a little shorter than the one before
    if (animationFrame == 1) framesSinceSwap = 0;
    if (++framesSinceSwap >= strobeSpeed / 2) {
      framesSinceSwap = 0;
      buildupGold = !buildupGold;
    }
    if (buildupGold) {
      strip.fill(strip.Color(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B));
    } else {
      strip.fill(strip.Color(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B));
    }
  } else {
    // Transition to explosion
    currentState = STATE_WIN_EXPLOSION;
    animationFrame = 0;
  }
  strip.show();
}

// One blast front moving out from the centre: brightest at the front, with a fading trail behind it.
void drawBlast(int front, uint8_t baseR, uint8_t baseG, uint8_t baseB) {
  const int trail = 24;
  int center = LED_COUNT / 2;
  for (int i = 0; i < center; i++) {
    int behind = front - i;
    if (behind < 0 || behind >= trail) continue;
    float brightness = 1.0f - (float)behind / trail;
    brightness = brightness * brightness;
    uint32_t color = strip.Color(baseR * brightness, baseG * brightness, baseB * brightness);
    strip.setPixelColor(center + i, color);
    strip.setPixelColor(center - 1 - i, color);
  }
}

void winExplosion() {
  if (animationFrame == 1) startSong(JACKPOT_EXPLOSION);
  // A gold blast, then a red one chasing it out to both ends
  if (animationFrame < 60) {
    strip.clear();
    drawBlast(animationFrame * 2, COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B);
    if (animationFrame >= 20) drawBlast((animationFrame - 20) * 2, COLOR_RED_R, COLOR_RED_G, COLOR_RED_B);
  } else {
    fadeTo(STATE_WIN_CHASE);
    return;
  }
  strip.show();
}

void winCheers() {
  if (animationFrame == 1) startSong(JACKPOT_CHEERS);
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
    fadeTo(STATE_WIN_BUILDUP);
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
      if (i >= glassWidth - FOAM_LEDS) { r = COLOR_FOAM_R; g = COLOR_FOAM_G; b = COLOR_FOAM_B; }  // head faces the middle
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
      if (i < FOAM_LEDS) { r = COLOR_FOAM_R; g = COLOR_FOAM_G; b = COLOR_FOAM_B; }  // head faces the middle
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
  if (animationFrame == 1) startSong(JACKPOT_CHASE);
  // Dual chasing lights from both ends
  if (animationFrame < 120) {
    strip.clear();
    
    int chaseWidth = 8;
    int pos1 = (animationFrame * 2) % LED_COUNT;
    int pos2 = (LED_COUNT * 2 - (animationFrame * 2)) % LED_COUNT;  // 120 frames at 2 LEDs each: two laps, never negative
    
    for (int i = 0; i < chaseWidth; i++) {
      int p1 = (pos1 + i) % LED_COUNT;
      int p2 = (pos2 - i + LED_COUNT) % LED_COUNT;
      
      uint32_t color1 = strip.Color(COLOR_RED_R, COLOR_RED_G, COLOR_RED_B);
      uint32_t color2 = strip.Color(COLOR_GOLD_R, COLOR_GOLD_G, COLOR_GOLD_B);
      
      strip.setPixelColor(p1, color1);
      strip.setPixelColor(p2, color2);
    }
  } else {
    fadeTo(STATE_WIN_SPARKLE);
    return;
  }
  strip.show();
}

void winSparkle() {
  if (animationFrame == 1) startSong(JACKPOT_SPARKLE);
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
    fadeTo(STATE_ATTRACT);
    return;
  }
  strip.show();
}
