# XrteShop — MVP de plataforma de comercio electrónico multivendedor (marketplace)

XrteShop es una plataforma de comercio electrónico multivendedor diseñada para conectar compradores con vendedores independientes.

**Estado: En desarrollo / MVP** · [Ver demo](https://xrteshop.vercel.app)

## Funcionalidades

- Exploración del catálogo con filtros y páginas de detalle de productos.
- Carrito de compras persistente.
- Registro, autenticación de usuarios y gestión de pedidos.
- Paneles separados para vendedores y administración.
- Integración con la API oficial de Mercado Libre para importar publicaciones y sincronizar inventario.
- Integración de pagos con Wompi en entorno de pruebas (sandbox).

## Alcance actual

El proyecto está en fase **MVP / desarrollo activo**. Wompi opera únicamente en **sandbox**: los pagos reales están deshabilitados. Los pagos y liquidaciones a vendedores, así como las compras a proveedores externos, son procesos manuales que se registran en la plataforma; no hay liquidaciones ni compras automatizadas.

## Stack tecnológico

- **Backend:** Python, Flask
- **Frontend:** HTML5, CSS3 y JavaScript (ES6+)
- **Bases de datos:** PostgreSQL (Neon) para producción y SQLite para desarrollo local
- **Integraciones y despliegue:** API oficial de Mercado Libre, Wompi (sandbox) y Vercel

## Estructura del proyecto

```text
XrteShop/
├── app.py
├── admin_security.py
├── catalog_cleanup_service.py
├── database_backend.py
├── mercadolibre_catalog_service.py
├── mercadolibre_oauth.py
├── mercadolibre_service.py
├── payment_gateway.py
├── payment_service.py
├── wompi_provider.py
├── docs/
│   └── marketplace-compatibility.md
├── scripts/
│   ├── build_vercel.py
│   ├── catalog_cleanup.py
│   ├── create_admin.py
│   ├── initialize_postgres.py
│   └── migrate_sqlite_to_postgres.py
├── static/
│   ├── css/
│   ├── images/
│   └── js/
├── templates/
├── tests/
├── .env.example
├── .gitignore
├── pyproject.toml
├── requirements.txt
└── vercel.json
```

`public/` se genera para el despliegue en Vercel a partir de `static/` y no forma parte del código fuente.

## Instalación y ejecución local

Requisitos: Python 3.14 o superior y Git.

1. Clona el repositorio y entra al directorio:

   ```powershell
   git clone https://github.com/jeanortega-co/XrteShop.git
   cd XrteShop
   ```

2. Crea y activa un entorno virtual:

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

3. Instala las dependencias:

   ```powershell
   pip install -r requirements.txt
   ```

4. Crea tu configuración local:

   ```powershell
   Copy-Item .env.example .env
   ```

   Edita `.env` y configura una base de datos. Para desarrollo local con SQLite, define `PEMU_DATABASE=pemu.sqlite3`; para PostgreSQL/Neon, configura `DATABASE_URL`. Define también `SECRET_KEY` y `PEMU_JWT_SECRET` con valores aleatorios e independientes. Para probar pagos, configura las credenciales de prueba `WOMPI_*`, define `WOMPI_ENV=sandbox` y mantén `WOMPI_PRODUCTION_APPROVED=0`. Las credenciales OAuth de Mercado Libre (`MERCADOLIBRE_*`) son necesarias solo si vas a conectar una cuenta.

   Puedes generar un secreto con:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Ejecuta el comando dos veces para obtener secretos distintos. No guardes valores reales en `.env.example` ni compartas el archivo `.env`.

5. Inicia el servidor Flask:

   ```powershell
   python app.py
   ```

   Abre [http://127.0.0.1:5000](http://127.0.0.1:5000).

## Dependencias

`requirements.txt` contiene las dependencias Python de ejecución: Flask, cryptography, requests, python-dotenv, bcrypt y psycopg con el adaptador binario. Sus rangos se mantienen alineados con `pyproject.toml`.

## Autor

**Jean Ortega** · [GitHub @jeanortega-co](https://github.com/jeanortega-co)
