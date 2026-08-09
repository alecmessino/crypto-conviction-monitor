-- Module B — emission versus adoption, as an observational feed.
--
-- Save this as a query in your Dune account and put its numeric ID in the repository
-- secret DUNE_UNLOCK_QUERY_ID. DUNE_API_KEY authenticates the read. The nightly calls
-- /query/{id}/results, so the query must be *saved and executed* at least once — the
-- results endpoint returns the last execution, not a fresh run.
--
-- Expected output columns (the fetcher accepts the aliases listed in DUNE_ALIASES):
--
--   symbol               text     upper- or lower-case, matched case-insensitively
--   supply_increase_pct  double   30-day circulating-supply growth, percent
--   addr_growth_pct      double   30-day active-address growth, percent
--   unlocks_usd          double   value of scheduled unlocks in the next 30 days
--
-- The nightly derives era = supply_increase_pct / addr_growth_pct if the query does not
-- return it, and records unlock_overhang_pct and adoption_dilution beside it.
--
-- ============================================================================
-- WHAT THIS QUERY CAN AND CANNOT GIVE YOU — read before trusting the column
-- ============================================================================
--
-- Supply growth and address growth are genuinely on-chain and this query computes them
-- honestly from transfer data.
--
-- **Scheduled unlocks are not on-chain in the general case.** A vesting cliff is a
-- contractual fact; it only appears on-chain if the project used an on-chain vesting
-- contract, and most large allocations do not. There is no canonical Dune table of
-- token unlock schedules. So `unlocks_usd` below is computed ONLY for tokens whose
-- vesting contracts you enumerate in the `vesting_contracts` CTE — it will be NULL for
-- everything else, and that NULL is honest rather than a gap to be filled with an
-- estimate. If you want broad unlock coverage you need an off-chain schedule source
-- (TokenUnlocks, Messari) joined in; do not let this query imply coverage it lacks.
--
-- The pipeline is built for that: a null is recorded as a null, the field-presence
-- monitor reports what share of the board carries a value, and nothing imputes.
--
-- ============================================================================

WITH
-- Chains to include. Add rows as you extend coverage; each must exist as a
-- tokens_<chain>.transfers table in Dune.
chains AS (
    SELECT * FROM (VALUES ('ethereum'), ('arbitrum'), ('base')) AS t(chain)
),

-- 30-day active addresses, and the 30 days before that, so growth is a ratio of two
-- equal windows rather than a level.
addr_now AS (
    SELECT
        t.symbol,
        COUNT(DISTINCT t."from") AS addrs
    FROM tokens_ethereum.transfers t
    WHERE t.block_time >= NOW() - INTERVAL '30' DAY
    GROUP BY 1
),
addr_prev AS (
    SELECT
        t.symbol,
        COUNT(DISTINCT t."from") AS addrs
    FROM tokens_ethereum.transfers t
    WHERE t.block_time >= NOW() - INTERVAL '60' DAY
      AND t.block_time <  NOW() - INTERVAL '30' DAY
    GROUP BY 1
),

-- Circulating supply proxied by net issuance: transfers out of the zero address are
-- mints, transfers into it are burns. This is a proxy — it does not know about
-- off-chain custody or bridged supply — and it is labelled as one in the terminal.
supply_now AS (
    SELECT
        t.symbol,
        SUM(CASE WHEN t."from" = 0x0000000000000000000000000000000000000000
                 THEN t.amount ELSE 0 END)
      - SUM(CASE WHEN t.to   = 0x0000000000000000000000000000000000000000
                 THEN t.amount ELSE 0 END) AS net_minted
    FROM tokens_ethereum.transfers t
    WHERE t.block_time >= NOW() - INTERVAL '30' DAY
    GROUP BY 1
),
supply_base AS (
    SELECT
        t.symbol,
        SUM(CASE WHEN t."from" = 0x0000000000000000000000000000000000000000
                 THEN t.amount ELSE 0 END)
      - SUM(CASE WHEN t.to   = 0x0000000000000000000000000000000000000000
                 THEN t.amount ELSE 0 END) AS net_minted
    FROM tokens_ethereum.transfers t
    WHERE t.block_time < NOW() - INTERVAL '30' DAY
    GROUP BY 1
),

-- On-chain vesting contracts you have identified, by token. Everything not listed here
-- gets a NULL unlock figure rather than a guess. Extend deliberately.
vesting_contracts AS (
    SELECT * FROM (VALUES
        -- (symbol, vesting contract address)
        -- ('EXAMPLE', 0x0000000000000000000000000000000000000000)
        (CAST(NULL AS VARCHAR), CAST(NULL AS VARBINARY))
    ) AS t(symbol, contract)
    WHERE symbol IS NOT NULL
),
unlocks AS (
    SELECT
        v.symbol,
        SUM(tr.amount_usd) AS unlocks_usd
    FROM vesting_contracts v
    JOIN tokens_ethereum.transfers tr
      ON tr."from" = v.contract
     AND tr.block_time >= NOW() - INTERVAL '30' DAY
    GROUP BY 1
)

SELECT
    UPPER(a.symbol)                                        AS symbol,
    -- Percent growth over the prior equal window. NULL when there is no prior window to
    -- compare against, rather than 0 — a token with no history has unknown growth, not
    -- flat growth, and the difference matters to anything that ranks on it later.
    CASE WHEN sb.net_minted > 0
         THEN ROUND(100.0 * sn.net_minted / sb.net_minted, 4) END  AS supply_increase_pct,
    CASE WHEN ap.addrs > 0
         THEN ROUND(100.0 * (an.addrs - ap.addrs) / ap.addrs, 4) END AS addr_growth_pct,
    u.unlocks_usd                                          AS unlocks_usd
FROM addr_now an
JOIN addr_prev ap USING (symbol)
LEFT JOIN supply_now  sn USING (symbol)
LEFT JOIN supply_base sb USING (symbol)
LEFT JOIN unlocks     u  USING (symbol)
CROSS JOIN (SELECT 1) AS _
JOIN addr_now a ON a.symbol = an.symbol
WHERE an.addrs >= 100          -- below this the growth ratio is noise
ORDER BY symbol
