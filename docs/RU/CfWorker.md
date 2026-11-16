# Cloudflare Worker

Альтернативный (полностью бесплатный, не нужно покупать домен в отличие от
[CfProxy](./CfProxy.md)) способ проксирования.

Прокси возвращает доступ к тому, что раньше не загружалось (реакции, некоторые
стикеры). Если на аккаунте без Premium с данным способом всё ещё не
загружаются фото/видео, оставьте в блоке «Маршруты к ЦОД» только
`4: 149.154.167.220`.

## Настройка Worker'а

1. **Добавьте в [zapret](https://github.com/Flowseal/zapret-discord-youtube/)
   или в любое другое ПО следующие домены:**

```
cloudflare.com
cloudflare.dev
workers.dev
```

2. Создайте аккаунт в [Cloudflare](https://dash.cloudflare.com/) (или войдите
   в существующий).
   * **После создания аккаунта подтвердите почту письмом, которое пришло на
     email.**
3. Слева в панели выберите `Compute` → `Workers & Pages`.
4. Нажмите сверху справа **`Create application`** → `Start with Hello World!`
   → `Deploy`.
5. Сверху справа нажмите **`Edit code`**, замените код слева на тот,
   [что находится внизу этой страницы](#код-workerа).
   * Если код не загружается, значит не выполнен первый пункт.
6. Нажмите сверху справа **`Deploy`**.
7. Скопируйте домен из поля справа (например,
   `random-symbols-1234.username.workers.dev`).

## Указание домена в tgwsproxy

Откройте веб-интерфейс роутера (`http://<router>:1434`) → блок
**«Fake TLS & Cloudflare fallback»** → поле **«CF Worker домены»**.

Можно указать несколько доменов через запятую — они пробуются по очереди,
а пул подключений привязывается к тому, который сработал:

```
my-worker.workers.dev, backup-worker.workers.dev
```

То же самое в `config.json` (`/opt/etc/tgwsproxy/config.json`):

```json
"cfproxy_worker_domains": ["my-worker.workers.dev", "backup-worker.workers.dev"]
```

После сохранения сервис перезапустится автоматически.

## Код Worker'а

```javascript
import { connect } from "cloudflare:sockets";

function toBytes(data) {
	if (data instanceof ArrayBuffer) {
		return new Uint8Array(data);
	}
	if (typeof data === "string") {
		return new TextEncoder().encode(data);
	}
	if (data && typeof data.arrayBuffer === "function") {
		return data.arrayBuffer().then((ab) => new Uint8Array(ab));
	}
	return new Uint8Array();
}

export default {
	async fetch(request) {
		if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
			return new Response("Expected websocket", { status: 426 });
		}

		const url = new URL(request.url);
		if (url.pathname !== "/apiws") {
			return new Response("Not found", { status: 404 });
		}

		const dst = url.searchParams.get("dst");
		const pair = new WebSocketPair();
		const client = pair[0];
		const server = pair[1];
		server.accept();

		const socket = connect({ hostname: dst, port: 443 });
		const tcpReader = socket.readable.getReader();
		const tcpWriter = socket.writable.getWriter();

		server.addEventListener("message", async (event) => {
			try {
				await tcpWriter.write(await toBytes(event.data));
			} catch {
				try {
					server.close(1011, "tcp write failed");
				} catch {}
			}
		});

		server.addEventListener("close", async () => {
			try {
				await tcpWriter.close();
			} catch {}
			try {
				socket.close();
			} catch {}
		});

		(async () => {
			try {
				while (true) {
					const { value, done } = await tcpReader.read();
					if (done) {
						break;
					}
					if (value) {
						server.send(value);
					}
				}
			} catch {
			} finally {
				try {
					server.close();
				} catch {}
				try {
					tcpReader.releaseLock();
				} catch {}
				try {
					socket.close();
				} catch {}
			}
		})();

		return new Response(null, { status: 101, webSocket: client });
	},
};
```
