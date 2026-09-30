# SDEX XLM Accumulator Bot

> **⚠️ WARNING: EXPERIMENTAL SOFTWARE**  
> This project is experimental software designed for automated trading on the Stellar Decentralized Exchange (SDEX). Use at your own risk. Always test thoroughly with testnet or minimal capital before operating with live funds. note: Don't use the populated grid_state.json as this records my live trades on my public key. The script will generate one for you. 

## Overview

A deterministic grid-trading script designed to accumulate XLM using a fixed USDC capital base on the Stellar SDEX. The bot manages capital in $1.00 tranches, placing trailing buy bids on market dips and staging consolidated sell offers at target profit margins (+1.0%).

## Key Features

* **Atomic Execution:** Submits trailing buys and staged sell offers together in single atomic transactions.
* **State Persistence:** Automatically tracks open positions, active offer IDs, and Horizon trade cursors via `grid_state.json`.
* **Self-Healing:** Auto-detects and re-maps active DEX orders to internal state on restart.
* **No Hardcoded Keys:** Utilizes environment variables for account credentials.

## Setup & Configuration

### Prerequisites
* Python 3.10+
* `stellar-sdk`

### Environment Variables
Set your secret key in your environment or GitHub Repository Secrets:

* `STELLAR_SECRET_KEY`: Stellar account secret key starting with `S...`

## Usage

### Direct Execution
```bash
python sdex_xlm_accumulator.py
