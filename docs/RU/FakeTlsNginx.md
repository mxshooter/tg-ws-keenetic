# Fake TLS + upstream в nginx

Домен из поля `fake_tls_domain` должен указывать на тот же IP, на котором
запущен прокси (обычно — IP роутера в LAN или ваш внешний адрес).

## Настройка в tgwsproxy

Веб-интерфейс → **«Fake TLS & Cloudflare fallback»** → **«Fake-TLS домен»**
(например, `example.com`). Галочка **«PROXY protocol v1»** находится в блоке
**«Производительность и логи»**; включите её, если прокси стоит за
nginx/haproxy.

То же самое в `config.json`:

```json
"fake_tls_domain": "example.com",
"proxy_protocol": true
```

Прокси по умолчанию слушает `0.0.0.0:1433`; в примере ниже nginx принимает
TLS на `:443` и передаёт трафик на `127.0.0.1:1433`.

## Пример `nginx.conf` для stream-модуля

```nginx
upstream mtproto {
    server 127.0.0.1:1433;
}

map $ssl_preread_server_name $sni_name {
    hostnames;
    example.com mtproto;
    # если есть xray с selfsni:
    # sub.example.com  www;
    # default xray;
}

# upstream xray {
#     server 127.0.0.1:8443;
# }
#
# upstream www {
#     server 127.0.0.1:7443;
# }

server {
    proxy_protocol on;
    set_real_ip_from unix:;
    listen          443;
    proxy_pass      $sni_name;
    ssl_preread     on;
}
```

## Ссылка для подключения

При заполненном `fake_tls_domain` веб-интерфейс выдаёт `ee`-секрет:

```text
tg://proxy?server=your.domain.com&port=443&secret=ee<secret><domain_hex>
```

Ссылку можно скопировать из карточки подключения в веб-интерфейсе.
