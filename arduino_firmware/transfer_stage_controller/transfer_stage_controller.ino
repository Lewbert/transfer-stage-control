// ============================================================
// Transfer Stage Controller — Non-Blocking 3-Axis Firmware
// ============================================================
// Controls a SigmaKoki XYZ stage via Arduino + Autonics MD5-HD14
// stepper drivers.  Uses micros()-based non-blocking pulse
// generation so all 3 axes can move simultaneously and the
// serial interface stays responsive at all times.
//
// Protocol: colon-delimited text commands, newline-terminated.
// Baud: 115200, 8N1
//
// Pin assignments:
//   XPUL=8, XDIR=9, XLIM_POS=5, XLIM_NEG=4
//   YPUL=10, YDIR=11, YLIM_POS=2, YLIM_NEG=3
//   ZPUL=12, ZDIR=13, ZLIM_POS=6, ZLIM_NEG=7
//
// Direction logic: X axis is normal.  Y and Z axes are INVERTED
// (matching the original calibration — see original source code).
// ============================================================

#include <avr/wdt.h>

// ------------------------------------------------------------------
// Pin Definitions
// ------------------------------------------------------------------
struct AxisPins {
  byte pul;
  byte dir;
  byte lim_pos;
  byte lim_neg;
};

const AxisPins pins[3] = {
  { 8, 9,  5, 4 },   // X axis  (POS/NEG swapped vs reference — hardware-specific)
  { 10, 11, 2, 3 },  // Y axis  (matches reference pin definition)
  { 12, 13, 6, 7 },  // Z axis  (matches reference pin definition)
};

// ------------------------------------------------------------------
// Speed Table — level → pulse half-period in microseconds
// ------------------------------------------------------------------
// Level 0:  20 ms half-period →  25 steps/sec
// Level 1:  10 ms half-period →  50 steps/sec
// Level 2:   5 ms half-period → 100 steps/sec
// Level 3:   3 ms half-period → ~167 steps/sec
// Level 4:   2 ms half-period → 250 steps/sec
// Level 5:   1 ms half-period → 500 steps/sec
// ------------------------------------------------------------------
const unsigned long SPEED_TABLE_US[6] = {
  20000, 10000, 5000, 3000, 2000, 1000
};

// ------------------------------------------------------------------
// Per-Axis State
// ------------------------------------------------------------------
struct MotorAxis {
  // Static config
  byte pul_pin, dir_pin, lim_pos_pin, lim_neg_pin;
  bool inverted;  // Y and Z are inverted

  // Motion state
  bool moving;
  int8_t direction;        // +1 (positive), -1 (negative), 0 (stopped)
  uint8_t speed_level;     // 0–5
  unsigned long half_period_us;

  // Timing
  unsigned long last_toggle_us;
  bool pulse_state;        // false=LOW, true=HIGH

  // Position tracking
  long position;

  // Single-step mode
  bool single_stepping;
  long single_steps_remaining;
};

MotorAxis axes[3];

// Axis name labels for messages
const char AXIS_NAMES[3] = {'X', 'Y', 'Z'};

// ------------------------------------------------------------------
// Serial buffer
// ------------------------------------------------------------------
const int MAX_CMD_LEN = 64;
char cmd_buffer[MAX_CMD_LEN];
int cmd_idx = 0;

// ------------------------------------------------------------------
// Watchdog
// ------------------------------------------------------------------
const unsigned long WDT_RESET_INTERVAL = 500;  // reset watchdog every 500ms
unsigned long last_wdt_reset = 0;

// ------------------------------------------------------------------
// Setup
// ------------------------------------------------------------------
void setup() {
  wdt_enable(WDTO_2S);  // 2-second watchdog

  for (int i = 0; i < 3; i++) {
    axes[i].pul_pin = pins[i].pul;
    axes[i].dir_pin = pins[i].dir;
    axes[i].lim_pos_pin = pins[i].lim_pos;
    axes[i].lim_neg_pin = pins[i].lim_neg;
    axes[i].inverted = (i >= 1);  // Y(1) and Z(2) are inverted
    axes[i].moving = false;
    axes[i].direction = 0;
    axes[i].speed_level = 2;  // default: 100 steps/sec
    axes[i].half_period_us = SPEED_TABLE_US[2];
    axes[i].last_toggle_us = 0;
    axes[i].pulse_state = false;
    axes[i].position = 0;
    axes[i].single_stepping = false;
    axes[i].single_steps_remaining = 0;

    pinMode(axes[i].pul_pin, OUTPUT);
    pinMode(axes[i].dir_pin, OUTPUT);
    pinMode(axes[i].lim_pos_pin, INPUT);
    pinMode(axes[i].lim_neg_pin, INPUT);

    digitalWrite(axes[i].pul_pin, LOW);
    digitalWrite(axes[i].dir_pin, LOW);
  }

  Serial.begin(115200);

  // Announce readiness
  Serial.println("BOOT");
  Serial.println("TRANSFER_STAGE_READY");
}

// ------------------------------------------------------------------
// Main Loop
// ------------------------------------------------------------------
void loop() {
  parseSerial();
  updateAllMotors();

  // Feed the watchdog
  unsigned long now = millis();
  if (now - last_wdt_reset >= WDT_RESET_INTERVAL) {
    wdt_reset();
    last_wdt_reset = now;
  }
}

// ------------------------------------------------------------------
// Serial Parsing — non-blocking, one char at a time
// ------------------------------------------------------------------
void parseSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmd_idx > 0) {
        cmd_buffer[cmd_idx] = '\0';
        handleCommand(cmd_buffer);
        cmd_idx = 0;
      }
    } else if (cmd_idx < MAX_CMD_LEN - 1) {
      cmd_buffer[cmd_idx++] = c;
    }
  }
}

// ------------------------------------------------------------------
// Command Dispatcher
// ------------------------------------------------------------------
void handleCommand(const char* cmd) {
  // Skip empty
  if (cmd[0] == '\0') return;

  // ---- PING ----
  if (strcmp(cmd, "PING") == 0) {
    Serial.println("PONG");
    return;
  }

  // ---- HOME (stop motors, reset position counters) ----
  if (strcmp(cmd, "HOME") == 0) {
    for (int i = 0; i < 3; i++) {
      stopAxis(i);
      axes[i].position = 0;
    }
    Serial.println("OK:HOME");
    return;
  }

  // ---- LIMITS? ----
  if (strcmp(cmd, "LIMITS?") == 0) {
    printLimits();
    return;
  }

  // ---- STATUS? ----
  if (strcmp(cmd, "STATUS?") == 0) {
    printStatus();
    return;
  }

  // ---- STOP:ALL ----
  if (strcmp(cmd, "STOP:ALL") == 0) {
    for (int i = 0; i < 3; i++) {
      stopAxis(i);
    }
    Serial.println("OK:STOP:ALL");
    return;
  }

  // ---- STOP:X / STOP:Y / STOP:Z ----
  if (cmd[0] == 'S' && cmd[1] == 'T' && cmd[2] == 'O' && cmd[3] == 'P' && cmd[4] == ':') {
    int idx = axisIndex(cmd[5]);
    if (idx >= 0) {
      stopAxis(idx);
      Serial.print("OK:STOP:");
      Serial.println(AXIS_NAMES[idx]);
    } else {
      Serial.println("ERR:BAD_AXIS");
    }
    return;
  }

  // ---- SPD:X:<level> ----
  if (cmd[0] == 'S' && cmd[1] == 'P' && cmd[2] == 'D' && cmd[3] == ':') {
    if (cmd[4] == 'A' && cmd[5] == 'L' && cmd[6] == 'L' && cmd[7] == ':') {
      // SPD:ALL:<level>
      int level = atoi(cmd + 8);
      if (level < 0) level = 0;
      if (level > 5) level = 5;
      for (int i = 0; i < 3; i++) {
        axes[i].speed_level = level;
        axes[i].half_period_us = SPEED_TABLE_US[level];
      }
      Serial.print("OK:SPD:ALL:");
      Serial.println(level);
    } else {
      // SPD:X:<level>
      int idx = axisIndex(cmd[4]);
      if (idx >= 0 && cmd[5] == ':') {
        int level = atoi(cmd + 6);
        if (level < 0) level = 0;
        if (level > 5) level = 5;
        axes[idx].speed_level = level;
        axes[idx].half_period_us = SPEED_TABLE_US[level];
        Serial.print("OK:SPD:");
        Serial.print(AXIS_NAMES[idx]);
        Serial.print(":");
        Serial.println(level);
      } else {
        Serial.println("ERR:BAD_AXIS");
      }
    }
    return;
  }

  // ---- STEP:X:<dir>:<steps> ----
  if (cmd[0] == 'S' && cmd[1] == 'T' && cmd[2] == 'E' && cmd[3] == 'P' && cmd[4] == ':') {
    int idx = axisIndex(cmd[5]);
    if (idx >= 0 && cmd[6] == ':') {
      int dir = atoi(cmd + 7);
      const char* colon2 = strchr(cmd + 7, ':');
      if (colon2 != NULL) {
        int steps = atoi(colon2 + 1);
        if (dir != 1 && dir != -1) {
          Serial.println("ERR:BAD_DIR");
          return;
        }

        // Reject if axis is already moving
        if (axes[idx].moving) {
          Serial.print("ERR:");
          Serial.print(AXIS_NAMES[idx]);
          Serial.println(":BUSY");
          return;
        }

        // Check limit in the direction of travel
        if (dir > 0 && digitalRead(axes[idx].lim_pos_pin) == HIGH) {
          Serial.print("ERR:");
          Serial.print(AXIS_NAMES[idx]);
          Serial.println(":LIMIT");
          return;
        }
        if (dir < 0 && digitalRead(axes[idx].lim_neg_pin) == HIGH) {
          Serial.print("ERR:");
          Serial.print(AXIS_NAMES[idx]);
          Serial.println(":LIMIT");
          return;
        }

        // Set up non-blocking single-step via updateAllMotors()
        setDirection(idx, dir);
        axes[idx].direction = dir;
        axes[idx].moving = true;
        axes[idx].single_stepping = true;
        axes[idx].single_steps_remaining = abs(steps);
        axes[idx].last_toggle_us = micros();
        axes[idx].pulse_state = false;

        Serial.print("OK:STEP:");
        Serial.print(AXIS_NAMES[idx]);
        Serial.print(":");
        Serial.println(steps);
      } else {
        Serial.println("ERR:BAD_FORMAT");
      }
    } else {
      Serial.println("ERR:BAD_AXIS");
    }
    return;
  }

  // ---- MV:X:<dir>:<spd>  (continuous move) ----
  if (cmd[0] == 'M' && cmd[1] == 'V' && cmd[2] == ':') {
    int idx = axisIndex(cmd[3]);
    if (idx >= 0 && cmd[4] == ':') {
      int dir = atoi(cmd + 5);
      const char* colon2 = strchr(cmd + 5, ':');
      int spd = colon2 ? atoi(colon2 + 1) : axes[idx].speed_level;
      if (spd < 0) spd = 0;
      if (spd > 5) spd = 5;

      if (dir == 1 || dir == -1) {
        int result = startContinuous(idx, dir, spd);
        if (result == 0) {
          Serial.print("OK:MV:");
          Serial.print(AXIS_NAMES[idx]);
          Serial.print(":");
          Serial.print(dir);
          Serial.print(":");
          Serial.println(spd);
        } else {
          Serial.print("ERR:");
          Serial.print(AXIS_NAMES[idx]);
          Serial.println(":LIMIT");
        }
      } else {
        Serial.println("ERR:BAD_DIR");
      }
    } else {
      Serial.println("ERR:BAD_AXIS");
    }
    return;
  }

  // Unknown
  Serial.print("ERR:UNKNOWN:");
  Serial.println(cmd);
}

// ------------------------------------------------------------------
// Motor Control
// ------------------------------------------------------------------

int axisIndex(char name) {
  if (name == 'X' || name == 'x') return 0;
  if (name == 'Y' || name == 'y') return 1;
  if (name == 'Z' || name == 'z') return 2;
  return -1;
}

void stopAxis(int idx) {
  axes[idx].moving = false;
  axes[idx].direction = 0;
  axes[idx].single_stepping = false;
  axes[idx].single_steps_remaining = 0;
  digitalWrite(axes[idx].pul_pin, LOW);
  axes[idx].pulse_state = false;
}

int startContinuous(int idx, int8_t dir, uint8_t spd) {
  // Check limit in the direction of travel
  if (dir > 0 && digitalRead(axes[idx].lim_pos_pin) == HIGH) return -1;
  if (dir < 0 && digitalRead(axes[idx].lim_neg_pin) == HIGH) return -1;

  // Set direction
  setDirection(idx, dir);

  // Configure speed
  axes[idx].speed_level = spd;
  axes[idx].half_period_us = SPEED_TABLE_US[spd];

  // Start motion
  axes[idx].direction = dir;
  axes[idx].moving = true;
  axes[idx].single_stepping = false;
  axes[idx].single_steps_remaining = 0;
  axes[idx].last_toggle_us = micros();
  axes[idx].pulse_state = false;

  return 0;
}

void setDirection(int idx, int8_t dir) {
  // Set direction pin with 10µs settling time
  if (dir > 0) {
    digitalWrite(axes[idx].dir_pin, axes[idx].inverted ? LOW : HIGH);
  } else {
    digitalWrite(axes[idx].dir_pin, axes[idx].inverted ? HIGH : LOW);
  }
  delayMicroseconds(10);
}

// ------------------------------------------------------------------
// Non-Blocking Motion Update — called every loop iteration
// ------------------------------------------------------------------
void updateAllMotors() {
  unsigned long now = micros();

  for (int i = 0; i < 3; i++) {
    if (!axes[i].moving) continue;

    unsigned long elapsed = now - axes[i].last_toggle_us;
    if (elapsed >= axes[i].half_period_us) {
      // Time to toggle the PUL pin
      axes[i].last_toggle_us = now;

      if (axes[i].pulse_state) {
        // Was HIGH, go LOW (second half of pulse)
        digitalWrite(axes[i].pul_pin, LOW);
        axes[i].pulse_state = false;

        // Single-step tracking (decrement on each full pulse cycle)
        if (axes[i].single_stepping) {
          axes[i].single_steps_remaining--;
          if (axes[i].single_steps_remaining <= 0) {
            stopAxis(i);
            continue;
          }
        }
      } else {
        // About to send rising edge — check limit FIRST
        if (axes[i].direction > 0 && digitalRead(axes[i].lim_pos_pin) == HIGH) {
          stopAxis(i);
          Serial.print("EV:LIM:");
          Serial.print(AXIS_NAMES[i]);
          Serial.println("+");
          continue;
        }
        if (axes[i].direction < 0 && digitalRead(axes[i].lim_neg_pin) == HIGH) {
          stopAxis(i);
          Serial.print("EV:LIM:");
          Serial.print(AXIS_NAMES[i]);
          Serial.println("-");
          continue;
        }

        // Rising edge: send pulse then update position
        digitalWrite(axes[i].pul_pin, HIGH);
        axes[i].pulse_state = true;
        axes[i].position += axes[i].direction;
      }
    }
  }
}

// ------------------------------------------------------------------
// Status / Query Helpers
// ------------------------------------------------------------------

void printLimits() {
  Serial.print("L:");
  for (int i = 0; i < 3; i++) {
    Serial.print(AXIS_NAMES[i]);
    Serial.print("+:");
    Serial.print(digitalRead(axes[i].lim_pos_pin));
    Serial.print(",");
    Serial.print(AXIS_NAMES[i]);
    Serial.print("-:");
    Serial.print(digitalRead(axes[i].lim_neg_pin));
    if (i < 2) Serial.print(",");
  }
  Serial.println();
}

void printStatus() {
  Serial.print("S:");
  for (int i = 0; i < 3; i++) {
    Serial.print(AXIS_NAMES[i]);
    Serial.print(":");
    Serial.print(axes[i].position);
    Serial.print(",");
  }
  Serial.print("XSPD:");
  Serial.print(axes[0].speed_level);
  Serial.print(",YSPD:");
  Serial.print(axes[1].speed_level);
  Serial.print(",ZSPD:");
  Serial.print(axes[2].speed_level);
  Serial.println();
}
