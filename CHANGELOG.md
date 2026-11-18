# Changelog

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
