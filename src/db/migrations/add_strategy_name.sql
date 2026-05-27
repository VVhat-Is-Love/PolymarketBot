-- Migration: add strategy_name column to live_trades and paper_trades
-- Run once on existing databases to track which strategy produced each trade.
-- Values: 'basket_wide' | 'basket_narrow' | 'tail_no' | 'manual'

ALTER TABLE live_trades ADD COLUMN strategy_name TEXT DEFAULT 'basket_wide';
ALTER TABLE paper_trades ADD COLUMN strategy_name TEXT DEFAULT 'basket_wide';
