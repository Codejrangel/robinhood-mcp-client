# Robinhood MCP — tool inventory

Tool inventory of Robinhood's agentic trading MCP server, observed 2026-09-16.
The server may add, rename, or remove tools over time — run `rh_mcp.py tools`
after login for the live list.

## Account & portfolio
- get_accounts
- get_portfolio
- get_equity_positions
- get_option_positions
- get_realized_pnl

## Equities / stocks
- get_equity_quotes
- get_equity_fundamentals
- get_equity_historicals
- get_equity_orders
- get_equity_tradability
- search

## Options
- get_option_chains
- get_option_instruments
- get_option_quotes
- get_option_historicals
- get_option_positions
- get_option_orders
- get_option_watchlist

## Order actions
- review_equity_order
- place_equity_order
- cancel_equity_order
- review_option_order
- place_option_order
- cancel_option_order

Note: orders go through a review_* step before place_*. That review-then-place
split is a guardrail layer — nothing gets placed without an explicit review pass.

## Scanners / screeners
- get_scans
- create_scan
- run_scan
- update_scan_filters
- update_scan_config

## Watchlists
- get_watchlists
- get_watchlist_items
- get_popular_watchlists
- create_watchlist
- update_watchlist
- add_to_watchlist
- remove_from_watchlist
- follow_watchlist
- unfollow_watchlist
- get_option_watchlist
- add_option_to_watchlist
- remove_option_from_watchlist

## Earnings
- get_earnings_results
- get_earnings_calendar

## Market indexes
- get_indexes
- get_index_quotes
