// Année automatique dans le pied de page
document.getElementById("annee").textContent = new Date().getFullYear();

// Menu mobile
const menuToggle = document.querySelector(".menu-toggle");
const nav = document.querySelector(".nav");

menuToggle.addEventListener("click", () => {
    nav.classList.toggle("ouvert");
});

// Fermer le menu après un clic sur un lien
nav.querySelectorAll("a").forEach((lien) => {
    lien.addEventListener("click", () => nav.classList.remove("ouvert"));
});

// Formulaire de contact
const form = document.getElementById("contact-form");
const formMessage = document.getElementById("form-message");

form.addEventListener("submit", (event) => {
    event.preventDefault();
    const nom = document.getElementById("nom").value;
    formMessage.textContent = `Merci ${nom}, ton message a bien été reçu !`;
    form.reset();
});
