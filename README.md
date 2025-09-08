# Lighter.xyz Delta Neutral Volume Generator Bot

A professional trading bot that generates volume on Lighter.xyz using a delta-neutral strategy with two accounts.

## Strategy Overview

This bot implements a delta-neutral trading strategy:
- Uses two separate accounts
- Opens opposing positions (long/short) simultaneously
- Maintains market neutrality
- Closes positions after random intervals (5-35 minutes)
- Executes 70-180 sessions daily

## Features

- **Delta Neutral**: Market-neutral strategy minimizes directional risk
- **Volume Generation**: Creates consistent trading volume
- **Risk Management**: Position size limits and automatic error recovery
- **Professional Architecture**: Async/await, proper logging, and error handling
- **Configurable**: Easy to adjust parameters for different trading preferences

## Installation

1. Clone the repository:
```bash
git clone https://github.com/your-username/lighter-volume-bot.git
cd lighter-volume-bot
