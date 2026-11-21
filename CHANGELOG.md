# Changelog

## 0.2.0

Laundry liquid (TwinDos) control, and a start time the washer keeps itself.

- **`set_program` takes `start_at`** (a date and time). The delay is written to the washer
  and its own timer is armed, the way the Miele app does a delayed start: the washer shows
  the countdown, waits, and starts by itself — also when Home Assistant is offline at that
  moment. Counted in whole minutes; refused when the washer does not offer a delayed start
  for the selected program; a moment in the past starts at once.
- After a start or a delayed start the entities are refreshed before the action returns, so
  an automation can read `sensor.<machine>_status` right after the call.
- **`wash_started` and `wash_duration` count from the real start.** With a delayed start the
  wash record still opens when the washer begins waiting, but the time on the washer's timer
  is no longer counted as washing.
- Seen on a real wash: delayed start, running and "finished" (Status 7), with Mobile Start
  reading on until the wash ends.

- **`select.<machine>_twindos`** with Off, Whites and Colours. It shows the choice the washer
  currently holds, and reads unknown when the washer shows a combination that was never
  measured. Nothing is guessed.
- **`switch.<machine>_detergent`**: off washes without laundry liquid; on brings back the last
  Whites or Colours choice the integration saw on this washer, and is refused with a clear
  message when it has seen none.
- **`set_program` takes `twindos`** (`off` / `whites` / `colours`) **and `detergent`**
  (true / false). Giving both with values that contradict each other is refused before
  anything is written. The result is read back from the machine like every other option.
- **Written whole or not at all.** A TwinDos change is only sent when the washer reports
  exactly two TwinDos containers and both can be changed, for off as well as on.
- **Raw diagnostic sensors** `sensor.<machine>_twindos_1_dosing` and `_twindos_2_dosing`.
- **The wash record keeps the laundry liquid:** `twindos` (raw values) and `twindos_mode`
  attributes on `sensor.<machine>_wash_extras`, and `binary_sensor.<machine>_wash_detergent`.
  Wash records stored by 0.1.1 still load; their laundry liquid is unknown.
- Example dashboard: a "Laundry liquid" row.

- **Measured power profiles** from a power-measuring plug chosen in the new options
  (**Configure**: a power sensor and optionally an energy sensor per washing machine; empty =
  no plug, and then nothing new appears). While the washer runs (not while it waits for a
  delayed start) the plug's samples become one time-weighted value per minute (Wh); minutes
  without a valid reading are stored as missing, never as 0. A finished wash with at most
  10 % missing minutes is stored under the key of its program, temperature, eco, spin,
  soiling and extras (`Cottons|20|eco=0|1600|soil=2|none`); TwinDos is not part of the key.
  The 5 most recent runs per key are kept in Home Assistant's storage; a wash running during
  a restart keeps its measured minutes.
- New sensors, only with a plug: `sensor.<machine>_wash_energy` (kWh of the current / last
  wash, per-minute detail as attributes), `_power_profile` (profile of the selected
  combination), `_power_profiles` (the whole matrix for templates), diagnostic `_power_plug`.
- **`miele_mobile_control.forget_profile`** removes a stored profile (default: the one shown).
- Example dashboard: a power profile card read from `sensor.<machine>_power_profile`.
- Existing entity ids, unique ids and behaviour are unchanged.

The three TwinDos combinations (Off = 0/0, Whites = 1/0, Colours = 0/2) were measured on one
WSI863 with UltraPhase 1 and UltraPhase 2 in its containers, program Cottons. Other detergent
configurations are untested.

## 0.1.1

The wash keeps its details.

- **A wash record per washer**, frozen the moment the program begins (waiting to start or
  running) from the last program context read while Mobile Start was armed. One sensor per
  detail, all kept during and after the run: `wash_program`, `wash_temperature` (°C, `eco`
  attribute), `wash_spin_speed`, `wash_soiling`, `wash_extras` (readable names plus one
  boolean attribute per offered extra), `wash_started`, `wash_finished`, `wash_duration`
  and `wash_state` (`none` / `running` / `finished` / `interrupted`). A new wash replaces
  the record.
- **One `wash_<extra>` binary sensor per extra** (17): on when that extra was selected for
  the recorded wash, each chartable and usable in automations.
- **The record survives a Home Assistant restart**: it is restored into the coordinator
  before the first poll and the wash sensors are RestoreSensors.
- **Started at the machine without the integration seeing a program?** The record takes
  the program from `/State` `ProgramID` and marks temperature, spin, soiling and extras
  unknown — nothing is invented.
- **Live readings while the program runs**: `target_temperature`, `current_spin_speed`,
  `elapsed_time`, the raw `program_phase` number (diagnostic sensor, no invented names) and
  `start_time` for a delayed start.
- Existing entity ids, unique ids and behaviour are unchanged; new unique ids follow
  `f"{fab}_{key}"`.

No real wash has been run through this integration yet: everything after "program selected"
is covered by tests only, and whether the program context can still be read while the washer
waits or runs is untested (the record does not depend on it). See the README limitations.

## 0.1.0

First release.

- Sign in with the e-mail and password of your Miele account, the one the Miele app uses.
- One device per washing machine on the account, with:
  - selects for program, temperature, spin speed and soiling, showing only what the selected program offers;
  - a switch per extra (Extra quiet, Soak, Extra gentle, Quick, SingleWash, Water plus, Pre-wash, Rinse hold,
    Pre-ironing, AllergoWash, Intensive and more);
  - status, remaining time, finish time, Mobile Start and door sensors, which work whether or not the machine
    is armed;
  - a Start button.
- `miele_mobile_control.set_program`: program, temperature, spin, soiling and extras in one action, with or
  without start.
- Controls work only while Mobile Start is armed at the machine. A start happens only when you press the button
  or call the action with `start: true`.
- Every change is validated against what the program offers and read back from the machine. Changes in quick
  succession are applied one after another.
- Example dashboard built from stock cards in `examples/dashboard.yaml`.
- Talks only to Miele's cloud (`rest-eu.domestic.miele-iot.com`).

Tested on a Miele WSI863 (Wi-Fi module EK057, firmware 08.37). Starting from Home Assistant has not been
exercised on a real machine in this release; see the limitations in the README.
