let db = [];

fetch("acis.json")
  .then(r => r.json())
  .then(json => db = json);

function search() {
  const key = document.getElementById("keyword").value;
  const result = db.filter(row =>
    row.sakumotsu.includes(key) ||
    row.byochu.includes(key) ||
    row.tsusho.includes(key)
  );

  document.getElementById("result").textContent =
    JSON.stringify(result, null, 2);
}
