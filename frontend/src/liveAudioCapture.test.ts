import { afterEach, describe, expect, it, vi } from 'vitest';
import { createLiveAudioCapture } from './liveAudio';

describe('createLiveAudioCapture', () => {
  const originalAudioContext = window.AudioContext;
  const originalAudioWorkletNode = globalThis.AudioWorkletNode;

  afterEach(() => {
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: originalAudioContext,
    });
    Object.defineProperty(globalThis, 'AudioWorkletNode', {
      configurable: true,
      value: originalAudioWorkletNode,
    });
    vi.restoreAllMocks();
  });

  it('uses AudioWorklet for capture and releases all audio resources', async () => {
    class FakeNode {
      connect = vi.fn();
      disconnect = vi.fn();
    }

    class FakePort {
      onmessage: ((event: MessageEvent<Float32Array>) => void) | null = null;
      close = vi.fn();
    }

    let context: FakeAudioContext | null = null;
    let worklet: FakeAudioWorkletNode | null = null;

    class FakeAudioWorkletNode extends FakeNode {
      port = new FakePort();

      constructor() {
        super();
        worklet = this;
      }
    }

    class FakeAudioContext {
      sampleRate = 4;
      destination = new FakeNode();
      source = new FakeNode();
      analyser = Object.assign(new FakeNode(), {
        fftSize: 128,
        frequencyBinCount: 64,
        getByteFrequencyData: vi.fn(),
      });
      audioWorklet = { addModule: vi.fn(async () => undefined) };
      close = vi.fn(async () => undefined);

      constructor() {
        context = this;
      }

      createMediaStreamSource() {
        return this.source;
      }

      createAnalyser() {
        return this.analyser;
      }
    }

    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    Object.defineProperty(globalThis, 'AudioWorkletNode', {
      configurable: true,
      value: FakeAudioWorkletNode,
    });
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:worklet');
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const onWindow = vi.fn();

    const cleanup = await createLiveAudioCapture({} as MediaStream, onWindow, {
      windowSec: 2,
      hopSec: 1,
    });

    expect(context?.audioWorklet.addModule).toHaveBeenCalledWith('blob:worklet');
    expect(revokeSpy).toHaveBeenCalledWith('blob:worklet');
    worklet?.port.onmessage?.({ data: Float32Array.from([0, 1, 2, 3, 4, 5, 6, 7]) } as MessageEvent<Float32Array>);
    expect(onWindow).toHaveBeenCalledTimes(1);

    cleanup();

    expect(worklet?.port.close).toHaveBeenCalled();
    expect(context?.source.disconnect).toHaveBeenCalled();
    expect(context?.analyser.disconnect).toHaveBeenCalled();
    expect(worklet?.disconnect).toHaveBeenCalled();
    expect(context?.close).toHaveBeenCalled();
  });
});
