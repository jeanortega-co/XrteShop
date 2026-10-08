# Pemu

Tienda en Flask con catálogo, cuentas, carrito, pedidos, pagos alojados por Mercado Pago y pagos de marketplace por vendedor.

## Configurar Mercado Pago

Los pagos reales necesitan una aplicación de Mercado Pago Marketplace y una URL pública HTTPS. No introduzcas claves en el código ni pidas números de tarjeta en formularios de Pemu. El comprador paga en el checkout alojado por Mercado Pago; cada vendedor conecta su cuenta mediante OAuth y configura allí su medio de retiro.

1. Crea una aplicación Marketplace en Mercado Pago Developers y configura su URL de retorno OAuth como `https://TU-DOMINIO/mercadopago/oauth/callback`.
2. Registra el webhook de pagos en `https://TU-DOMINIO/mercadopago/webhook` y configura la clave de firma que entrega Mercado Pago.
3. Copia `.env.example` a `.env` y completa las variables usando las credenciales de prueba primero. Genera dos secretos independientes:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   El primer valor es para `PEMU_SECRET_KEY`; el segundo es para `PEMU_TOKEN_ENCRYPTION_KEY`. Guarda ambas claves en un administrador de secretos. Perder la clave Fernet impide descifrar las conexiones OAuth de los vendedores.

4. `PEMU_PUBLIC_URL` debe ser el origen HTTPS público exacto registrado en Mercado Pago. En desarrollo local se necesita un túnel HTTPS con URL estable y registrar esa misma URL de retorno y webhook.
5. Instala dependencias con `pip install -r requirements.txt` y reinicia Flask después de configurar las variables.
6. Cada vendedor conecta Mercado Pago desde **Vende en Pemu**. La cuenta de retiro se administra en Mercado Pago, no se almacena la cuenta bancaria ni el número de tarjeta en Pemu.

El checkout agrupa el carrito por vendedor y procesa cada grupo en un checkout seguro separado. Pemu retiene una comisión fija del 10% del valor de cada venta a través de `marketplace_fee` de Mercado Pago; el vendedor recibe el 90% antes de las tarifas de procesamiento que Mercado Pago pueda cobrar. Esta condición se informa en la página **Vende en Pemu**. Si un grupo del carrito se paga y otro falla, el pedido queda marcado como pago parcial para revisión: no se debe volver a cobrarlo hasta resolver esa diferencia.

Sin las credenciales reales y el dominio HTTPS del negocio, la integración permanece deshabilitada y no puede procesar cobros en vivo. Las credenciales de prueba no deben mezclarse con credenciales de producción.
