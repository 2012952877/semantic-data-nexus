---
name: Semantic Nexus
description: A governed analytics operating ledger for inspectable semantic runs
colors:
  registry-ink: "#17231d"
  mineral-paper: "#f6f5ef"
  ledger-paper: "#ebece3"
  signal-lime: "#d9ff55"
  rule-line: "#c9cdc3"
  error-rust: "#a13728"
  status-light-pending: "#4b574f"
  status-light-success: "#455d0e"
  status-light-empty: "#6f5006"
  status-light-failed: "#922f21"
  status-dark-pending: "#c0c8c2"
  status-dark-success: "#b9dd5f"
  status-dark-empty: "#f0cc6a"
  status-dark-failed: "#ff9485"
typography:
  display:
    fontFamily: "Aptos, Segoe UI Variable Text, Segoe UI, sans-serif"
    fontSize: "clamp(1.75rem, 3vw, 3rem)"
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: "-0.035em"
  body:
    fontFamily: "Aptos, Segoe UI Variable Text, Segoe UI, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
  data:
    fontFamily: "Cascadia Code, Consolas, monospace"
    fontSize: "0.72rem"
    fontWeight: 400
rounded:
  ledger: "2px"
spacing:
  compact: "8px"
  control: "16px"
  section: "28px"
components:
  button-primary:
    backgroundColor: "{colors.signal-lime}"
    textColor: "{colors.registry-ink}"
    rounded: "{rounded.ledger}"
    padding: "10px 15px"
  ledger-surface:
    backgroundColor: "{colors.mineral-paper}"
    textColor: "{colors.registry-ink}"
    rounded: "{rounded.ledger}"
---

# Design System: Semantic Nexus

## Overview

**Creative North Star: "The Accountable Operating Ledger"**

The interface behaves like a precise control ledger: every question becomes a traceable record, every stage occupies a ruled line, and evidence remains near the decision it supports. It rejects chat-first analytics and interchangeable card dashboards. Density is deliberate but plain-language explanations lead technical notation.

**Key characteristics:** registry-green operational fields; mineral-paper work areas; one signal-lime state color; square controls; ruled rows; restrained data typography.

## Colors

Registry ink carries operational surfaces, mineral paper carries composition, and signal lime is reserved for action or live state.

- **Registry Ink** (`#17231d`): navigation, execution spine, committed manifests.
- **Mineral Paper** (`#f6f5ef`): primary reading and input surface.
- **Ledger Paper** (`#ebece3`): table headings and quiet grouping.
- **Signal Lime** (`#d9ff55`): primary action, active navigation, live stage.
- **Rule Line** (`#c9cdc3`): structural separators.
- **Error Rust** (`#a13728`): failed execution only.
- **Status inks:** dark, contrast-checked state colors on paper; lighter counterparts on registry ink.

**The One Signal Rule.** Lime marks the next meaningful action or current execution state, never decoration.

## Typography

UI copy uses the native Aptos/Segoe family for high-density Chinese and Latin text. Cascadia Code is limited to IDs, measurements, node kinds, checksums, and locations.

- **Display:** 700 weight, `clamp(1.75rem, 3vw, 3rem)`, 1.05 line-height.
- **Body:** regular weight, 1.6 line-height, generally no wider than 70 characters.
- **Label:** 700–900 weight at 0.68–0.82rem.
- **Data:** monospaced at 0.65–0.78rem.

## Layout

Desktop uses a compact 88px rail and asymmetric ask workspace: the paper composer leads, while a narrower dark execution spine records progress. Content pages use a maximum 1360px ledger. Below 1120px, ask and execution stack. Below 760px, navigation becomes a persistent four-item bottom rail and the primary ask action remains directly above it.

## Elevation & Depth

Depth is structural rather than ambient. Important writable or committed surfaces use a small down-right offset shadow; ordinary sections are separated by tonal fields and rules.

## Shapes

Corners stay nearly square at 2px. One-pixel rules and occasional three-pixel top rules define hierarchy. Pills, circular icon tiles, decorative glass, and generic rounded cards are outside the system.

## Components

- **Primary button:** lime, ink text, 2px corners, hard down-right shadow; disabled state becomes flat gray.
- **Secondary button:** paper field with registry border.
- **Ledger section:** mineral paper, strong top rule, compact structural shadow.
- **Stage register:** ordered ruled rows with index, bilingual label, duration, and text status.
- **Status badge:** square mark plus text; color never carries state alone.
- **Tables:** left-align dimensions, right-align measures, retain horizontal scroll on narrow screens.

## Do's and Don'ts

- Do make governance, state, and recovery visible beside the action.
- Do lead with Chinese plain language and keep the English technical term as support.
- Do use monospaced type only for machine-readable values.
- Don't collapse five stages into one indeterminate loader.
- Don't introduce gradients, glass decoration, oversized hero text, excessive pills, or emoji icons.
- Don't display secrets, real account data, or unlabelled non-synthetic business claims.
