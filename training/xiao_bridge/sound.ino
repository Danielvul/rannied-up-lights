// The jackpot sounds. A small synth plays metallic slot machine bells and coins, and the chip's I2S hardware turns
// them into a smooth signal on one pin. It runs in its own task, so the sound keeps perfect time whatever the LEDs or
// the camera link are doing.

#include <driver/i2s_pdm.h>

#define SPEAKER_PIN D1  // XC3744 S pin. D1 is the V2's SPI select, so the speaker only works with LINK_SPI 0
#define SAMPLE_RATE 24000
#define VOLUME 3        // loudness before the peaks get rounded off: higher is louder, but harsher
#define VOICES 8        // notes that can ring at once
#define BELL_RING 12    // how long bells ring: each extra 1 doubles it (12 = about 0.17 s to fade by two thirds)
#define COIN_RING 10    // coins are short
#define ATTACK_SAMPLES 24   // 1 ms: bells are struck
#define ECHO_SAMPLES 2400   // 0.1 s: the room

struct SongStep {
  uint16_t freqHz;      // 0 for a rest, COIN for a coin
  uint16_t durationMs;  // time until the next step: the note itself rings on past it
};

const uint16_t COIN = 1;  // a short clink at a random pitch
const uint16_t NOTE_C5 = 523, NOTE_D5 = 587, NOTE_E5 = 659, NOTE_F5 = 698, NOTE_G5 = 784, NOTE_A5 = 880, NOTE_B5 = 988;
const uint16_t NOTE_C6 = 1047, NOTE_D6 = 1175, NOTE_E6 = 1319, NOTE_F6 = 1397, NOTE_G6 = 1568, NOTE_A6 = 1760, NOTE_B6 = 1976;
const uint16_t NOTE_C7 = 2093, NOTE_E7 = 2637;

// Each list plays once as its light scene starts. Bells 1 ms apart sound together. Every list ends with {0, 0}.

#define COIN_RUN {COIN, 45}, {COIN, 70}, {COIN, 35}, {COIN, 60}, {COIN, 50}, {COIN, 80}  // 340 ms of coins
#define ALARM {NOTE_C7, 60}, {NOTE_G6, 60}, {NOTE_C7, 60}, {NOTE_G6, 60}                 // the jackpot bell
#define STAB {NOTE_C6, 1}, {NOTE_E6, 1}, {NOTE_G6, 1}, {NOTE_C7, 1}                      // a big bright chord

// Three reels stop, then the bells start as the glasses meet at 1.4 s
const SongStep JACKPOT_CHEERS[] = {
  {NOTE_G5, 300}, {NOTE_G5, 300}, {NOTE_C6, 800},
  ALARM, ALARM,
  {0, 0},
};

// Bells climbing two octaves
const SongStep JACKPOT_BUILDUP[] = {
  {NOTE_C5, 80}, {NOTE_D5, 80}, {NOTE_E5, 80}, {NOTE_F5, 80}, {NOTE_G5, 80}, {NOTE_A5, 80}, {NOTE_B5, 80},
  {NOTE_C6, 80}, {NOTE_D6, 80}, {NOTE_E6, 80}, {NOTE_F6, 80}, {NOTE_G6, 80}, {NOTE_A6, 80}, {NOTE_B6, 80},
  {0, 0},
};

// JACKPOT: a big chord and the alarm bell
const SongStep JACKPOT_EXPLOSION[] = {
  STAB, {NOTE_E7, 240},
  ALARM, ALARM, ALARM, ALARM,
  {0, 0},
};

// The payout: coins pour out with a bell over the top
const SongStep JACKPOT_CHASE[] = {
  {NOTE_C7, 1}, COIN_RUN, {NOTE_G6, 1}, COIN_RUN, {NOTE_C7, 1}, COIN_RUN, {NOTE_G6, 1}, COIN_RUN,
  {NOTE_C7, 1}, COIN_RUN, {NOTE_G6, 1}, COIN_RUN, {NOTE_C7, 1}, COIN_RUN, {NOTE_G6, 1}, COIN_RUN,
  {0, 0},
};

// More coins, slowing to the last few
const SongStep JACKPOT_SPARKLE[] = {
  COIN_RUN, COIN_RUN, COIN_RUN, COIN_RUN,
  {COIN, 90}, {COIN, 120}, {COIN, 100}, {COIN, 160}, {COIN, 140}, {COIN, 220},
  {0, 0},
};

struct Voice {
  uint32_t phase, step;
  uint32_t shinePhase, shineStep;  // the second tone that makes it sound like metal
  int32_t level, peak, rise;       // level: 0 to 1<<24
  uint8_t ring;
  bool coin;
};

i2s_chan_handle_t speaker;
int16_t wave[1024];
Voice voices[VOICES];
uint8_t nextVoice = 0;
volatile const SongStep* wantedSong = nullptr;  // loop() sets it, the sound task plays it
volatile bool beepWanted = false;

uint32_t phaseStep(uint32_t hz) {
  return (uint32_t)(((uint64_t)hz << 32) / SAMPLE_RATE);
}

void noteOn(uint16_t hz) {
  bool coin = hz == COIN;
  Voice& v = voices[nextVoice];
  nextVoice = (nextVoice + 1) % VOICES;
  v.coin = coin;
  v.step = phaseStep(coin ? random(1500, 2600) : hz);
  // Not a harmonic of the note, so it rings like metal: 2.75 times for bells, a different clash for every coin
  v.shineStep = coin ? v.step / 100 * random(135, 165) : v.step / 4 * 11;
  v.phase = v.shinePhase = 0;
  v.level = 0;
  v.peak = coin ? 1 << 23 : 1 << 24;
  v.rise = v.peak / ATTACK_SAMPLES;
  v.ring = coin ? COIN_RING : BELL_RING;
}

int32_t mixVoices() {
  int32_t mix = 0;
  for (Voice& v : voices) {
    if (v.rise) {
      v.level += v.rise;
      if (v.level >= v.peak) {
        v.level = v.peak;
        v.rise = 0;
      }
    } else {
      v.level -= v.level >> v.ring;
    }
    if (v.level < 4096) continue;
    v.phase += v.step;
    v.shinePhase += v.shineStep;
    int32_t level = v.level >> 9;
    // Bells: the second tone dies away twice as fast as the note. Coins: both tones equal, so they clash
    int32_t shine = v.coin ? level * 2 : (level * level) >> 15;
    mix += (wave[v.phase >> 22] * level + wave[v.shinePhase >> 22] * shine / 2) >> 15;
  }
  return mix;
}

// Adds the echo, then rounds off the peaks when notes stack up, rather than letting them clip.
int16_t nextSample() {
  static int16_t echo[ECHO_SAMPLES];
  static uint16_t position = 0;
  int32_t dry = mixVoices() + echo[position] / 4;
  echo[position] = constrain(dry, -32000, 32000);
  position = (position + 1) % ECHO_SAMPLES;
  int32_t out = dry * VOLUME;
  if (out > 22000) out = 22000 + (out - 22000) / 4;
  if (out < -22000) out = -22000 + (out + 22000) / 4;
  return constrain(out, -32000, 32000);
}

void soundTask(void*) {
  static int16_t block[240];  // 10 ms
  const SongStep* song = nullptr;
  const SongStep* next = nullptr;  // the step to play, null when the part has finished
  uint32_t samplesLeft = 0;
  for (;;) {
    const SongStep* wanted = (const SongStep*)wantedSong;
    if (wanted != song) {
      song = next = wanted;
      samplesLeft = 0;
    }
    if (beepWanted) {
      beepWanted = false;
      noteOn(880);
    }
    for (int i = 0; i < 240; i++) {
      if (next && !samplesLeft) {
        if (next->durationMs) {
          if (next->freqHz) noteOn(next->freqHz);
          samplesLeft = (uint32_t)next->durationMs * SAMPLE_RATE / 1000;
          next++;
        } else {
          next = nullptr;
        }
      }
      if (samplesLeft) samplesLeft--;
      block[i] = nextSample();
    }
    size_t written;
    i2s_channel_write(speaker, block, sizeof(block), &written, portMAX_DELAY);
  }
}

void startSound() {
  for (int i = 0; i < 1024; i++) wave[i] = 11000 * sinf(i * 2 * PI / 1024);
  i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  channel.dma_desc_num = 4;
  channel.dma_frame_num = 240;
  channel.auto_clear = true;
  i2s_pdm_tx_clk_config_t clock = I2S_PDM_TX_CLK_DAC_DEFAULT_CONFIG(SAMPLE_RATE);
  i2s_pdm_tx_slot_config_t slots = I2S_PDM_TX_SLOT_DAC_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
  i2s_pdm_tx_config_t config = {};
  config.clk_cfg = clock;
  config.slot_cfg = slots;
  config.gpio_cfg.clk = I2S_GPIO_UNUSED;
  config.gpio_cfg.dout = (gpio_num_t)SPEAKER_PIN;
  esp_err_t err = i2s_new_channel(&channel, &speaker, nullptr);
  if (!err) err = i2s_channel_init_pdm_tx_mode(speaker, &config);
  if (!err) err = i2s_channel_enable(speaker);
  if (err) {
    Serial.printf("Speaker failed to start: %s\n", esp_err_to_name(err));
    return;
  }
  xTaskCreate(soundTask, "sound", 3072, nullptr, 3, nullptr);
  Serial.println("Speaker ready");
}

void bootBeep() {  // shows the speaker works
  beepWanted = true;
}

void startSong(const SongStep* song) {
  wantedSong = song;
}

void stopSong() {  // whatever's ringing fades out by itself
  wantedSong = nullptr;
}
