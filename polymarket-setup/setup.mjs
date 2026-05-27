// polymarket-setup/setup.mjs
import { createWalletClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";
import { ClobClient } from "@polymarket/clob-client-v2";

const PRIVATE_KEY    = "0x3c5cf61e027b09b4605c1c8cefbf489fdb2fa13709725b0b3f784e4d74a78330";
const DEPOSIT_WALLET = "0x5817FCb2300Fb88bd36e30e6EC01FA595d466d4F";

const account = privateKeyToAccount(PRIVATE_KEY);
const wallet  = createWalletClient({
  account, chain: polygon,
  transport: http("https://rpc-mainnet.matic.quiknode.pro"),
});

const client = new ClobClient({
  host: "https://clob.polymarket.com",
  chain: 137, signer: wallet, funder: DEPOSIT_WALLET,
});

// Смотрим реальную структуру ответа
const creds = await client.createOrDeriveApiKey();
console.log("Полный объект creds:");
console.log(JSON.stringify(creds, null, 2));
console.log("\nКлючи объекта:", Object.keys(creds));

// Пробуем все варианты имён
const key = creds.apiKey ?? creds.api_key ?? creds.key ?? creds.ApiKey;
const secret = creds.apiSecret ?? creds.api_secret ?? creds.secret ?? creds.Secret;
const pass = creds.apiPassphrase ?? creds.api_passphrase ?? creds.passphrase;

console.log("\n=== Найденные значения ===");
console.log("key:        ", key);
console.log("secret:     ", secret);
console.log("passphrase: ", pass);

if (key) {
  console.log("\n=== Скопируй в .env ===");
  console.log(`POLYMARKET_API_KEY=${key}`);
  console.log(`POLYMARKET_API_SECRET=${secret}`);
  console.log(`POLYMARKET_API_PASSPHRASE=${pass}`);
  console.log(`PROXY_WALLET_ADDRESS=${DEPOSIT_WALLET}`);
}