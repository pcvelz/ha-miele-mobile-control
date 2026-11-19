# Changelog

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
