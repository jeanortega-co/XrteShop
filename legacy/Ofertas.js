document.addEventListener('DOMContentLoaded', function() {
    let cart = JSON.parse(localStorage.getItem('cart')) || [];
    const cartCount = document.getElementById('cart-count');
    const cartList = document.getElementById('cart-list');
    const simpleCart = document.getElementById('simple-cart');
    const cartLink = document.querySelector('.nav-links li:nth-child(4) a');
    const clearCartButton = document.getElementById('clear-cart');
    const closeCartButton = document.getElementById('close-cart');
  
   
    updateCart();
    
   
    cartLink.addEventListener('click', function(e) {
      e.preventDefault();
      simpleCart.classList.toggle('visible');
    });
  
    
    closeCartButton.addEventListener('click', function() {
      simpleCart.classList.remove('visible');
    });
  
   
    clearCartButton.addEventListener('click', function() {
      cart = [];
      updateCart();
    });
  
   
    document.querySelectorAll('.btn-comprar').forEach(button => {
      button.addEventListener('click', function() {
        const product = this.closest('.producto');
        const productName = product.querySelector('h3').textContent;
        const productPrice = parseFloat(product.querySelector('.precio-descuento').textContent.replace(/[^0-9.]/g, ''));
  
        const existingItem = cart.find(item => item.name === productName);
        if (existingItem) {
          existingItem.quantity++;
        } else {
          cart.push({
            name: productName,
            price: productPrice,
            quantity: 1
          });
        }
  
        updateCart();
        this.innerHTML = '<i class="fas fa-check"></i> Agregado';
        this.classList.add('added');
        setTimeout(() => {
          this.innerHTML = 'Comprar';
          this.classList.remove('added');
        }, 1500);
      });
    });
  
    function updateCart() {
        cartList.innerHTML = '';
        let total = 0;
      
        cart.forEach(item => {
          const li = document.createElement('li');
          li.innerHTML = `
            <span>${item.name} (${item.quantity})</span>
            <span>COP ${item.price * item.quantity}</span>
          `;
          total += item.price * item.quantity;
          cartList.appendChild(li);
        });
      
        cartCount.textContent = cart.reduce((sum, item) => sum + item.quantity, 0);
        localStorage.setItem('cart', JSON.stringify(cart));
      
        if (cart.length > 0) {
          simpleCart.classList.add('visible');
        } else {
          simpleCart.classList.remove('visible');
        }
      }
      
  });
  