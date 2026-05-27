// polymarket-setup/test_order.mjs
import { createWalletClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";
import { ClobClient, OrderType, Side } from "@polymarket/clob-client-v2";

const PRIVATE_KEY    = "0x3c5cf61e027b09b4605c1c8cefbf489fdb2fa13709725b0b3f784e4d74a78330";
const DEPOSIT_WALLET = "0x5817FCb2300Fb88bd36e30e6EC01FA595d466d4F";

const account = privateKeyToAccount(PRIVATE_KEY);
const wallet  = createWalletClient({
  account, chain: polygon,
  transport: http("https://rpc-mainnet.matic.quiknode.pro"),
});

const creds = {
  key:        "cc383e31-707c-967f-079f-f53c32037d06",
  secret:     "87wV0mDGzZNb68Dj5T-Is899lhOTTwlTkz8WCpjnWnw=",
  passphrase: "84320144f08666f8b6bbf9f73561681a8c7d3a5314668959d5be2849c5b45510",
};

const client = new ClobClient({
  host: "https://clob.polymarket.com",
  chain: 137, signer: wallet, funder: DEPOSIT_WALLET, creds,
});

// Шаг 1 — берём живой рынок
console.log("=== Ищем активный рынок ===");
const markets = await client.getSamplingMarkets();
const market = markets.data[0];
const TOKEN_ID = market.tokens[0].token_id;
const question = market.question?.slice(0, 50);
console.log(`Рынок: ${question}`);
console.log(`Token ID: ${TOKEN_ID}`);

// Шаг 2 — проверяем getTickSize на живом рынке
console.log("\n=== Tick size ===");
try {
  const ts = await client.getTickSize(TOKEN_ID);
  console.log("Tick size:", ts);
} catch(e) {
  console.log("getTickSize error:", e.message);
}

// Шаг 3 — пробуем getClobMarketInfo для прогрева кэша
console.log("\n=== Market info ===");
try {
  const info = await client.getClobMarketInfo(TOKEN_ID);
  console.log("Market info:", JSON.stringify(info));
} catch(e) {
  console.log("getClobMarketInfo error:", e.message);
}

// Шаг 4 — ордер с живым token_id
console.log("\n=== Тест ордера ===");
try {
  const order = await client.createOrder(
    { TOKEN_ID, price: 0.02, side: Side.BUY, size: 5 },
    { tickSize: "0.01", negRisk: false }
  );
  const resp = await client.postOrder(order, OrderType.GTC);
  console.log("✅ УСПЕХ:", JSON.stringify(resp));
} catch(e) {
  console.log("❌ Ошибка:", e.message);
}