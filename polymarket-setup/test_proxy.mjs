// polymarket-setup/test_proxy.mjs
import { createWalletClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";
import { ClobClient, OrderType, Side } from "@polymarket/clob-client-v2";

// СТАРЫЙ ключ и кошелёк
const PRIVATE_KEY   = "0x721824e8246312a4d03252fedf4b6cda7f516b8c0fec65f827b51e05e96efecb";
const PROXY         = "0x9Ce3E9a1e408B2c635674020db6Ab685294BAC2B";
const API_KEY       = "cc383e31-707c-967f-079f-f53c32037d06";
const API_SECRET    = "87wV0mDGzZNb68Dj5T-Is899lhOTTwlTkz8WCpjnWnw=";
const API_PASS      = "84320144f08666f8b6bbf9f73561681a8c7d3a5314668959d5be2849c5b45510";

const account = privateKeyToAccount(PRIVATE_KEY);
const wallet  = createWalletClient({
  account, chain: polygon,
  transport: http("https://rpc-mainnet.matic.quiknode.pro"),
});

const creds = { key: API_KEY, secret: API_SECRET, passphrase: API_PASS };

const client = new ClobClient({
  host: "https://clob.polymarket.com",
  chain: 137,
  signer: wallet,
  funder: PROXY,
  creds,
});

console.log("EOA:", account.address);
console.log("Proxy (funder):", PROXY);

// Баланс
console.log("\n=== Баланс ===");
try {
  const bal = await client.getBalanceAllowance({ assetType: "COLLATERAL" });
  console.log("Balance:", JSON.stringify(bal));
} catch(e) { console.log("Balance error:", e.message); }

// Рынок
const markets = await client.getSamplingMarkets();
const market = markets.data[0];
const TOKEN_ID = market.tokens[0].token_id;
console.log(`\nРынок: ${market.question?.slice(0,50)}`);
console.log(`Token ID: ${TOKEN_ID}`);

// Ордер
console.log("\n=== Тест ордера ===");
try {
  const order = await client.createOrder(
    { tokenID: TOKEN_ID, price: 0.02, side: Side.BUY, size: 5 },
    { tickSize: "0.01", negRisk: false }
  );
  console.log("Ордер создан");
  const resp = await client.postOrder(order, OrderType.GTC);
  console.log("✅ УСПЕХ:", JSON.stringify(resp));
} catch(e) {
  console.log("❌ Ошибка:", e.message);
  // Покажем что уходит в запросе
  console.log("Details:", JSON.stringify(e, null, 2));
}