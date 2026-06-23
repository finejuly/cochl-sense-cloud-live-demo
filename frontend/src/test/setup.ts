import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

HTMLCanvasElement.prototype.getContext = vi.fn(() => {
  return {
    beginPath: vi.fn(),
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    lineTo: vi.fn(),
    moveTo: vi.fn(),
    setTransform: vi.fn(),
    stroke: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
  } as unknown as CanvasRenderingContext2D;
});

afterEach(() => {
  cleanup();
});
