# OrkestaPay para Odoo

Proveedor de pago [OrkestaPay](https://orkestapay.com) para **Odoo 19.0** (Community y
Enterprise). El comprador paga con tarjeta en el checkout hospedado de OrkestaPay y regresa
a la tienda; Odoo confirma el pago consultando el API de OrkestaPay.

- Checkout hospedado (flujo *redirect*): Odoo no ve ni guarda datos de tarjeta.
- Monedas MXN y USD.
- Webhooks firmados (Svix) y conciliación periódica de pagos pendientes.
- Reembolsos totales y parciales desde Odoo.
- Depende solo del módulo `payment`; sin dependencias Python externas.

## Instalación

**Desde la Odoo Apps Store** (cuando esté publicado): busca *OrkestaPay* e instálalo.

**Desde este repositorio**: clona la rama `19.0` en una carpeta que esté en el
`addons_path` de tu Odoo, actualiza la lista de aplicaciones e instala *OrkestaPay Payment
Provider*.

```bash
git clone -b 19.0 https://github.com/orkestapay/odoo-payment.git
```

En Odoo.sh, agrégalo como submódulo de tu repositorio en la rama `19.0`.

## Configuración

Necesitas una cuenta de OrkestaPay y sus llaves del API (portal → *Desarrolladores → Llaves
del API*). Para probar, usa las del **sandbox**.

1. *Facturación (o Sitio web) → Configuración → Proveedores de pago → OrkestaPay*.
2. Pestaña *Credenciales*: captura `Client ID` y `Client Secret`, y pulsa **Probar
   conexión**.
3. Copia la *URL del webhook* que muestra el formulario y regístrala en el portal de
   OrkestaPay (*Desarrolladores → Webhooks → Add Endpoint*, eventos `payment.*`). Copia el
   *Signing Secret* del endpoint (`whsec_...`) al campo *Webhook Signing Secret*.
4. Cambia el estado a **Modo de prueba** (sandbox) o **Habilitado** (producción) y publícalo.

El sandbox y producción usan llaves distintas: al pasar a producción captura las llaves y el
Signing Secret de producción.

## Desarrollo local

La rama `main` incluye un entorno con Docker Compose (Odoo 19 + PostgreSQL 16) que monta
[`payment_orkestapay/`](payment_orkestapay/) como addon. Requisitos: Docker con Compose v2 y,
para recibir el regreso del comprador y los webhooks, [ngrok](https://ngrok.com/).

```bash
cp .env.example .env          # opcional: puerto, base de datos, nombre del proyecto
docker compose up -d          # Odoo en http://localhost:8069
docker compose logs -f odoo
```

Odoo corre con `--dev=reload,xml` (recarga Python y XML al guardar) y `--proxy-mode`
(respeta los headers `X-Forwarded-*` de ngrok). El gestor de bases de datos está
desactivado (`--no-database-list`) porque ngrok lo publicaría en internet; la interfaz web
solo sirve la base `ODOO_DB` (por defecto `odoo_poc`).

### Crear la base

```bash
docker compose run --rm odoo odoo -d odoo_poc --with-demo \
  -i website_sale,payment_orkestapay --stop-after-init
docker compose restart odoo
```

Entra con `admin` / `admin`. Después de cambiar el módulo, actualízalo con
`-u payment_orkestapay` en lugar de `-i`.

### Exponer Odoo con ngrok

OrkestaPay redirige al comprador y envía los webhooks a URLs construidas con
`web.base.url`, que debe ser la URL pública de ngrok:

1. `ngrok http 8069` y copia la URL `https://<nombre>.ngrok-free.dev` (el dominio fijo de tu
   cuenta de ngrok).
2. En Odoo, con el modo desarrollador: *Ajustes → Técnico → Parámetros del sistema*.
   `web.base.url` = la URL de ngrok (sin `/` final) y crea `web.base.url.freeze` = `True`.
3. Entra a Odoo **por la URL de ngrok** y registra la URL del webhook en el portal sandbox.

Si usas una URL que cambia en cada reinicio, repite el paso 2 y actualiza el endpoint del
webhook en el portal.

### Tests

```bash
# Suite del módulo en una base nueva con solo `payment` (se omiten los tests que requieren `sale`)
docker compose run --rm odoo odoo -d test_db -i payment_orkestapay \
  --test-tags /payment_orkestapay --stop-after-init

# Suite completa, incluidos los desgloses desde pedidos de venta
docker compose run --rm odoo odoo -d test_db_sale -i website_sale,payment_orkestapay \
  --test-tags /payment_orkestapay --stop-after-init

# Una clase o un método
docker compose run --rm odoo odoo -d test_db -u payment_orkestapay \
  --test-tags /payment_orkestapay:TestSvixSignature --stop-after-init
```

`-i` sobre una base donde el módulo ya está instalado no corre tests: usa `-u` o borra la
base (`docker compose stop odoo && docker compose exec -T db dropdb -U odoo test_db`).
Para empezar de cero: `docker compose down -v` (borra las bases y el filestore locales).

### Tarjetas de prueba (sandbox)

| Caso | Tarjeta |
|---|---|
| Aprobada | `4111 1111 1111 1111` |
| Rechazada | `4242 4242 4242 4242` |
| Desafío 3DS | `4000 0000 0000 2503` (cualquier fecha futura) |

## Ramas

| Rama | Uso |
|---|---|
| `main` | Desarrollo. Recibe los PRs y corre CI. Incluye el entorno Docker. |
| `19.0` | Versión publicada en la Odoo Apps Store. Solo contiene el módulo, este README y la licencia. Se actualiza en cada release; no se le hacen PRs. |

Cada release incrementa la versión del manifiesto (`19.0.x.y.z`) y se marca con un tag
(`v19.0.x.y.z`).

## Contribuir

Reporta errores o propuestas en los *issues* de este repositorio. Los PRs van contra `main`:

- Una rama por cambio (`fix/...`, `feat/...`) y tests para el comportamiento nuevo.
- Código y comentarios en inglés; los textos visibles para el usuario con `self.env._()`.
- Revisiones estáticas con [pre-commit](https://pre-commit.com/): pylint-odoo, flake8
  (líneas de hasta 99 caracteres) y prettier para XML. Instálalo una vez y se ejecutará en cada
  commit:
  ```bash
  pip install pre-commit
  pre-commit install
  pre-commit run --all-files    # todo el repositorio
  ```
- El CI de GitHub Actions corre los mismos hooks y, con la imagen `odoo:19.0`, instala el
  módulo (solo con `payment` y con `website_sale`), ejecuta los tests y lo desinstala. Para
  correrlo en tu máquina con la base del `docker-compose.yml`:
  ```bash
  docker run --rm --user root --network <proyecto>_default -v "$PWD":/src -w /src \
    -e DB_HOST=db --entrypoint bash odoo:19.0 \
    .github/scripts/odoo-ci.sh website_sale,payment_orkestapay
  ```
- Nunca subas credenciales: van en tu `.env` local (ignorado por git) o en el registro del
  proveedor en Odoo.

Si encuentras una vulnerabilidad, no abras un issue público: escribe a OrkestaPay por su
canal de soporte.

## Licencia

[LGPL-3](LICENSE).
