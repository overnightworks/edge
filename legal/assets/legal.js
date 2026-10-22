// The table of contents starts open for the two-column layout and collapses on a phone.
if (matchMedia("(max-width:899px)").matches) {
  document.querySelector("aside.toc details")?.removeAttribute("open");
}
