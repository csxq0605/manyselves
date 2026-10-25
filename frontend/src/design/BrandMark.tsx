// The module M: 13 modules on a 5 x 5 grid. The centre module is Main, the self the user talks to.
const cells: readonly (readonly [number, number])[] = [
  [0, 0], [0, 1], [0, 2], [0, 3], [0, 4], [4, 0], [4, 1], [4, 2], [4, 3], [4, 4], [1, 1], [3, 1], [2, 2],
];

export function BrandMark({ size = 24 }: { readonly size?: number }) {
  return (
    <svg aria-hidden="true" className="brand-mark" height={size} viewBox="0 0 48 48" width={size}>
      {cells.map(([column, row]) => (
        <rect
          className={column === 2 && row === 2 ? "brand-mark__main" : "brand-mark__cell"}
          height="8"
          key={`${column}-${row}`}
          rx="1.5"
          width="8"
          x={column * 10}
          y={row * 10}
        />
      ))}
    </svg>
  );
}
