export const appendAndParseSseBuffer = (buffer, chunk) => {
  let working = `${buffer || ""}${chunk || ""}`;
  const events = [];
  let boundary = working.indexOf("\n\n");
  while (boundary !== -1) {
    const raw = working.slice(0, boundary).trim();
    working = working.slice(boundary + 2);
    if (raw) {
      const dataLines = raw
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.replace(/^data:\s*/, ""));
      if (dataLines.length) {
        events.push(dataLines.join("\n"));
      }
    }
    boundary = working.indexOf("\n\n");
  }
  return { remainingBuffer: working, events };
};
