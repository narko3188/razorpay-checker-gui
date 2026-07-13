# Razorpay CC Checker GUI

CustomTkinter GUI for checking credit cards against Razorpay payment gateway.

## Features
- Import cards from .txt (format: CC|MM|YY|CVV)
- Paste cards directly
- Live result table with stats
- Export LIVE/ALL results
- Dark theme (black & gold)

## Requirements
```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage
```bash
python razorpay_gui.py
```

## Format
Cards file format: `CC|MM|YY|CVV`  (one per line)
```
4111111111111111|12|28|123
5500000000000004|01|27|456
```

## Author
**AngelGuardian** — [narko3188](https://github.com/narko3188)
